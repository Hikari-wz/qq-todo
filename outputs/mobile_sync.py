"""Independent phone service. Publish curated card content; phone owns statuses."""
import argparse
import base64
import hmac
import json
import os
import re
from datetime import datetime
from ipaddress import IPv4Address, IPv4Network
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from todo_server import Handler
from trial import ROOT, TIMES, read, save, set_manual_status

FIELDS = {"id", "title", "action", "notes", "condition", "decision", "times"}


def credentials(name):
    secret = os.environ.get(name, "")
    if len(secret) < 16 or not secret.isascii():
        raise ValueError(f"{name} 必须包含至少 16 个 ASCII 字符")
    return secret


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != {"revision", "cards"}:
        raise ValueError("发布文件只能包含 revision 和 cards")
    revision = payload["revision"]
    if type(revision) is not int or revision < 1 or not isinstance(payload["cards"], list):
        raise ValueError("版本或卡片格式无效")
    cards = {}
    for card in payload["cards"]:
        if not isinstance(card, dict) or set(card) != FIELDS:
            raise ValueError("卡片必须只含整理后的字段")
        key = card["id"]
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) or key in cards:
            raise ValueError("卡片 ID 无效或重复")
        if card["decision"] not in ("todo", "review"):
            raise ValueError("只发布待做和待确认")
        for field in ("title", "action", "notes", "condition"):
            if not isinstance(card[field], str) or len(card[field]) > 500:
                raise ValueError("卡片文字无效")
        times = card["times"]
        if not isinstance(times, dict) or set(times) != set(TIMES):
            raise ValueError("时间字段无效")
        for value in times.values():
            if value is not None:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?", value):
                    raise ValueError("时间格式无效")
                datetime.fromisoformat(value)
        cards[key] = {field: value for field, value in card.items() if field != "id"}
    return revision, cards


class Mobile(Handler):
    timeout = 10

    def authorized(self, publishing=False):
        expected = self.server.publish_auth if publishing else self.server.phone_auth
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        if hmac.compare_digest(supplied, expected):
            return True
        self.send_response(401)
        if not publishing:
            self.send_header("WWW-Authenticate", 'Basic realm="QQ Todo"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if self.path == "/healthz":
            try:
                read(self.server.state_path)
            except (OSError, ValueError):
                self.json(503, {"ok": False})
            else:
                self.json(200, {"ok": True})
            return
        publishing = self.path == "/api/publish"
        if not self.authorized(publishing):
            return
        state = read(self.server.state_path)
        if publishing:
            self.json(200, state)
        elif self.path == "/api/cards":
            self.json(200, [dict(card, id=key, evidence=[]) for key, card in state["cards"].items()])
        elif self.path == "/":
            self.send(200, (ROOT / "todo.html").read_bytes(), "text/html; charset=utf-8")
        else:
            self.json(404, {"error": "页面不存在"})

    def do_POST(self):
        publishing = self.path == "/api/publish"
        if not self.authorized(publishing):
            return
        if self.path not in ("/api/publish", "/api/status"):
            self.json(404, {"error": "页面不存在"})
            return
        if self.headers.get_content_type() != "application/json" or (
            not publishing and self.headers.get("Origin") != self.server.origin
        ):
            self.json(403, {"error": "请求来源或格式无效"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 512_000:
                raise ValueError("请求长度无效")
            payload = json.loads(self.rfile.read(length))
            with self.server.write_lock:
                state = read(self.server.state_path)
                if publishing:
                    revision, incoming = validate(payload)
                    if revision > state["revision"]:
                        for key, card in incoming.items():
                            card["manual_status"] = state["cards"].get(key, {}).get("manual_status")
                            state["cards"][key] = card
                        state["revision"] = revision
                        save(self.server.state_path, state)
                else:
                    set_manual_status(state, payload["id"], payload["status"])
                    save(self.server.state_path, state)
            self.json(200, {"ok": True, "revision": state["revision"]})
        except (ValueError, KeyError, TypeError) as error:
            self.json(400, {"error": str(error)})
        except OSError:
            self.json(500, {"error": "状态文件写入失败"})


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--state", required=True, type=Path)
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8876)
    serve.add_argument("--origin")
    for command in ("publish", "pull"):
        client = sub.add_parser(command)
        client.add_argument("--url", required=True)
        client.add_argument("--local-lan", action="store_true", help="显式允许 HTTP 到私有 IPv4 的演示服务")
        client.add_argument("--manifest", type=Path, required=command=="publish")
        if command == "pull":
            client.add_argument("--state", type=Path, help="写入供 Windows 页面读取的独立同步镜像")
    args = parser.parse_args()
    token = credentials("TODO_PUBLISH_TOKEN")
    if args.command == "serve":
        password = credentials("TODO_PHONE_PASSWORD")
        if not args.state.exists():
            save(args.state, {"revision": 0, "cards": {}})
        with ThreadingHTTPServer((args.bind, args.port), Mobile) as server:
            server.write_lock = Lock()
            server.state_path = args.state
            server.origin = args.origin or f"http://127.0.0.1:{server.server_port}"
            server.publish_auth = f"Bearer {token}".encode("ascii")
            server.phone_auth = b"Basic " + base64.b64encode(f"todo:{password}".encode("ascii"))
            print(server.origin, flush=True)
            server.serve_forever()
        return
    endpoint = urlsplit(args.url)
    allowed_lan = False
    if args.local_lan and endpoint.scheme == "http":
        try:
            address = IPv4Address(endpoint.hostname)
            allowed_lan = any(address in IPv4Network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
        except ValueError:
            pass
    if endpoint.scheme != "https" and not (endpoint.scheme == "http" and (endpoint.hostname == "127.0.0.1" or allowed_lan)):
        parser.error("远程同步必须使用 HTTPS")
    payload = read(args.manifest) if args.command == "publish" else None
    if payload is not None:
        validate(payload)
    request = Request(args.url.rstrip("/") + "/api/publish",
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
        result = json.load(response)
    if args.command == "pull" and args.state:
        mirror = {"mode": "mobile_mirror", "revision": result["revision"], "processed": {},
                  "cards": {key: dict(card, evidence=[]) for key, card in result["cards"].items()}}
        if args.state.exists() and read(args.state).get("mode") != "mobile_mirror":
            parser.error("目标文件已有其他用途，请指定独立同步镜像")
        save(args.state, mirror)
        print(f"同步版本 {result['revision']}，{len(mirror['cards'])} 张卡")
    else:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

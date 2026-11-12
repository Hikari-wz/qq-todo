"""Serve the QQ replay cards on this computer."""
import argparse
import base64
import hmac
import json
from http.cookies import CookieError, SimpleCookie
from time import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from contextlib import nullcontext
from ipaddress import IPv4Address

from group_filters import muted_card
from trial import ROOT, read, save, set_manual_status


APP_ASSETS = {
    "/manifest.webmanifest": "application/manifest+json",
    "/todo-icon-180.png": "image/png",
    "/todo-icon-192.png": "image/png",
    "/todo-icon-512.png": "image/png",
}


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def send(self, status, body, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, payload):
        self.send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def local_request(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.json(403, {"error": "请使用启动时显示的本机地址。"})
            return False
        return True

    def do_GET(self):
        asset_type = APP_ASSETS.get(self.path)
        if asset_type:
            self.send(200, (ROOT / self.path[1:]).read_bytes(), asset_type)
            return
        if not self.local_request():
            return
        if self.path == "/":
            self.send(200, (ROOT / "todo.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/health-note":
            with getattr(self.server, "state_lock", nullcontext()):
                state = read(self.server.state_path)
            self.json(200, {"extract_provider": getattr(self.server, "extract_provider", None),
                            "extract_ready": getattr(self.server, "extract_ready", False),
                            "warning": "历史补查可能不完整，请手动查看群内遗漏通知。"
                           if (state.get("history_check", {}).get("possibly_incomplete") or any(
                               g.get("active", True) and g.get("possibly_incomplete") for g in state.get("groups", {}).values())) else ""})
        elif self.path == "/api/cards":
            with getattr(self.server, "state_lock", nullcontext()):
                state = read(self.server.state_path)
            self.json(200, self.cards(state))
        else:
            self.json(404, {"error": "页面不存在。"})

    def sources(self, state, card):
        sources = []
        for item in card.get("evidence", []):
            key = item["id"]
            message = state.get("processed", {}).get(key, {}).get("message", {})
            group = message.get("group", "")
            group_id = group.removeprefix("QQ 群 ")
            group_name = state.get("groups", {}).get(group_id, {}).get("name")
            sources.append({
                "group": group_name or group,
                "at": message.get("at") or card.get("message_at"),
                "text": message.get("text") or item.get("quote", ""),
            })
        return sources

    def cards(self, state):
        return [
            dict(card, id=key, evidence=self.sources(state, card))
            for key, card in state["cards"].items() if not muted_card(card)
        ]

    def do_POST(self):
        if not self.local_request():
            return
        if self.path != "/api/status":
            self.json(404, {"error": "页面不存在。"})
            return
        origin = getattr(self.server, "origin", f"http://127.0.0.1:{self.server.server_port}")
        if self.headers.get("Origin") != origin or self.headers.get_content_type() != "application/json":
            self.json(403, {"error": "请从本机待办页面提交。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("请求长度无效。")
            body = json.loads(self.rfile.read(length))
            with getattr(self.server, "state_lock", nullcontext()):
                state = read(self.server.state_path)
                set_manual_status(state, body["id"], body["status"])
                save(self.server.state_path, state)
        except (ValueError, KeyError, TypeError) as error:
            self.json(400, {"error": str(error)})
            return
        except OSError:
            self.json(500, {"error": "保存失败，请稍后重试。"})
            return
        self.json(200, {"ok": True})


class LanHandler(Handler):
    cookie_name = "__Host-qqtodo"
    session_seconds = 30 * 24 * 60 * 60

    def session_signature(self, expires):
        payload = f"qqtodo-session:{self.server.origin}:{expires}".encode("ascii")
        return hmac.digest(self.server.phone_auth, payload, "sha256").hex()

    def has_session(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            expires, signature = cookies[self.cookie_name].value.split(".", 1)
            if not expires.isascii() or not expires.isdecimal() or len(expires) > 12:
                return False
            if not signature.isascii() or len(signature) != 64:
                return False
            return int(expires) > time() and hmac.compare_digest(
                signature, self.session_signature(expires)
            )
        except (CookieError, KeyError, ValueError):
            return False

    def end_headers(self):
        cookie = getattr(self, "_new_session_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
            self._new_session_cookie = None
        super().end_headers()

    def local_request(self):
        if (self.headers.get("Host") != self.server.origin.split("://", 1)[1]
                or IPv4Address(self.client_address[0]) not in self.server.lan_network):
            self.json(403, {"error": "仅允许指定局域网访问。"})
            return False
        secure = self.server.origin.startswith("https://")
        if secure and self.has_session():
            return True
        actual = self.headers.get("Authorization", "").encode("utf-8")
        if hmac.compare_digest(actual, self.server.phone_auth):
            if secure and self.command == "GET" and self.path == "/":
                expires = int(time()) + self.session_seconds
                token = f"{expires}.{self.session_signature(expires)}"
                self._new_session_cookie = (
                    f"{self.cookie_name}={token}; Path=/; Max-Age={self.session_seconds}; "
                    "Secure; HttpOnly; SameSite=Lax"
                )
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="QQ Todo LAN"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def cards(self, state):
        return [
            dict(id=key, title=card["title"], action=card["action"], notes="",
                 condition=card["condition"], times=card["times"], decision=card["decision"],
                 manual_status=card["manual_status"], evidence=self.sources(state, card),
                 message_at=card.get("message_at") if card.get("local_share") else None)
            for key, card in state["cards"].items() if card["decision"] in ("todo", "review") and not muted_card(card)
        ]


def phone_auth(password):
    if len(password) < 16 or not password.isascii():
        raise ValueError("局域网登录密码无效。")
    return b"Basic " + base64.b64encode(f"todo:{password}".encode("ascii"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not args.state.is_file():
        parser.error("请先准备回放状态文件。")
    with HTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.state_path = args.state.resolve()
        print(f"http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()



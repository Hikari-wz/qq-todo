"""Receive authorized NapCat groups using the existing loopback WebSocket."""
import argparse
import json
import os
import re
from time import perf_counter
from live_cloud import admitted, project, validate_times
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from contextlib import ExitStack
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from threading import Event, Lock, Thread

from group_filters import MUTED_GROUPS, muted_message
from todo_server import Handler, LanHandler, phone_auth
from trial import ROOT, TIMES, apply, infer, read, save

STATS = ("received", "filtered", "duplicate", "accepted", "processed", "failed", "created", "todo", "review", "image")
FILTER_REASONS = ("heartbeat", "non_message", "self_sent", "private", "other_group",
                  "no_allowlist", "invalid_message", "before_activation")
LOCAL_ZONE = timezone(timedelta(hours=8))


def group_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    return value if re.fullmatch(r"[1-9][0-9]{0,19}", value) else None


def normalize(event, allowed):
    if not allowed or event.get("post_type") != "message" or event.get("message_type") != "group":
        return None
    if group_id(event.get("group_id")) != allowed:
        return None
    account, message_id = event.get("self_id"), event.get("message_id")
    if group_id(account) is None or not re.fullmatch(r"-?[0-9]{1,24}", str(message_id)):
        return None
    segments = event.get("message")
    if not isinstance(segments, list):
        return None
    parts = []
    shares = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        data = segment.get("data")
        if segment.get("type") == "text" and isinstance(data, dict) and isinstance(data.get("text"), str):
            parts.append(data["text"])
        elif segment.get("type") == "image":
            parts.append("[图片]")
        elif segment.get("type") == "json":
            shares.append(segment)
    text = "".join(parts).strip()
    if shares and not text:
        text = "[分享卡片]"
    if not text:
        return None
    try:
        at = datetime.fromtimestamp(int(event["time"]), LOCAL_ZONE).isoformat(sep=" ")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
    return {"id": f"qq:{account}:{allowed}:{message_id}", "at": at, "group": f"QQ 群 {allowed}", "text": text, **({"local_shares": shares} if shares else {})}


class Pilot:
    def __init__(self, path, allowed, profile, model_port, all_groups=False):
        self.path = path
        self.allowed = allowed
        self.all_groups = all_groups
        self.syncing = False
        self.model_port = model_port
        self.cloud = os.environ.get("TODO_EXTRACT_PROVIDER") == "deepseek"
        self.lock = Lock()
        self.stop = Event()
        self.prompt = (ROOT / "prompt.txt").read_text(encoding="utf-8")
        self.model_profile = dict(profile)
        personal_path = ROOT / "personal-profile.json"
        if personal_path.is_file():
            personal = read(personal_path)
            if personal["班级"] == profile["班级"]:
                self.model_profile.update(personal)


        if all_groups:
            self.prompt += "\n当前是全部群模式。群成员身份不等于通知适用；班级、学校或活动身份缺失不能猜测。跨群仅在相同通知全文明确说明为同一事项时关联；同名不同事项不得合并。每个独立行动分别提取。"
        if path.exists():
            current = read(path)
            if current.get("mode") != "napcat" or (not all_groups and (current.get("all_groups") or current["allowed"] != allowed)) or current["profile"] != profile:
                raise ValueError("群号或身份不同，请使用新的试运行状态文件。")
        else:
            save(path, dict(mode="napcat", allowed=allowed, profile=profile, cards={},
                            processed={}, pending={}, stats=dict.fromkeys(STATS, 0)))

        if all_groups:
            current = read(path)
            if not current.get("all_groups"):
                current.update(all_groups=True, enabled_at=int(datetime.now(LOCAL_ZONE).timestamp()),
                               groups={}, group_list_ready=False)
            current["pending"] = {
                key: entry for key, entry in current["pending"].items() if not muted_message(key)
            }
            save(path, current)
            self.enabled_at = current["enabled_at"]

    def receive(self, event):
        post_type = event.get("post_type") if isinstance(event, dict) else None
        message = None
        if post_type not in ("message", "message_sent"):
            reason = ("heartbeat" if post_type == "meta_event" and
                      event.get("meta_event_type") == "heartbeat" else "non_message")
        elif post_type == "message_sent":
            reason = "self_sent"
        elif event.get("message_type") == "private":
            reason = "private"
        elif event.get("message_type") != "group" or group_id(event.get("group_id")) is None:
            reason = "invalid_message"
        elif group_id(event["group_id"]) in MUTED_GROUPS:
            reason = "muted_group"
        elif not self.allowed and not self.all_groups:
            reason = "no_allowlist"
        elif not self.all_groups and group_id(event["group_id"]) != self.allowed:
            reason = "other_group"
        else:
            message = normalize(event, group_id(event["group_id"]) if self.all_groups else self.allowed)
            reason = "invalid_message" if message is None else None
            if message and self.all_groups and int(event["time"]) < self.enabled_at:
                message, reason = None, "before_activation"
        with self.lock:
            state = read(self.path)
            stats = state["stats"]
            if message and self.all_groups:
                from all_groups import register_group
                progress = register_group(state, group_id(event["group_id"]), event.get("group_name", ""), int(event["time"]))
                progress["last_received"] = max(progress.get("last_received", 0), int(event["time"]))
                progress["last_message_id"] = str(event["message_id"])
            breakdown = state.setdefault("filter_counts", dict.fromkeys(FILTER_REASONS, 0))
            if post_type in ("message", "message_sent"):
                stats["received"] += 1
                if reason:
                    stats["filtered"] += 1
            if reason:
                breakdown[reason] = breakdown.get(reason, 0) + 1
            elif message["id"] in state["processed"] or message["id"] in state["pending"]:
                stats["duplicate"] += 1
            else:
                stats["accepted"] += 1
                state["pending"][message["id"]] = {"message": message, "attempts": 0}
            save(self.path, state)

    def extract(self, context):
        if not self.cloud:
            return infer(self.model_port, self.prompt, context, provider="local")
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise ValueError("DEEPSEEK_API_KEY missing")
        clock = perf_counter()
        metric = {"at": datetime.now(LOCAL_ZONE).isoformat(), "requested_model": "deepseek-flash"}
        try:
            result = infer(self.model_port, self.prompt + (
                "\n成绩、签到、选课未知则待确认。明确其他班跳过。"
                "quote须覆盖每个时间的原文依据；节次不换算钟点，结束时刻只取原文。"
            ), context, provider="deepseek", model="deepseek-flash", api_key=key)
            metric.update(model=result[2], usage=result[1], finish=result[0].get("finish_reason"))
            return result
        except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
            metric.update(error=type(error).__name__, http_status=getattr(error, "code", None))
            raise
        finally:
            metric["ms"] = round((perf_counter() - clock) * 1000)
            with self.path.with_name("deepseek-live-usage.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(metric) + "\n")

    def process_next(self):
        with self.lock:
            snapshot = read(self.path)
        if self.syncing or not snapshot["pending"]:
            return None
        ready = [(k, v) for k, v in snapshot["pending"].items()
                 if v.get("retry_after", 0) <= datetime.now(LOCAL_ZONE).timestamp()]
        if not ready:
            return None
        key, entry = min(ready, key=lambda pair: pair[1]["message"]["at"])
        message = entry["message"]
        raw = usage = None
        from all_groups import related_cards
        candidates = related_cards(snapshot, message) if self.all_groups else snapshot["cards"]
        try:
            if message.get("local_shares"):
                rows = []
            elif re.fullmatch(r"(?:[^。！？；\r\n:：\[\]]{1,60}[:：]\s*)?(?:\[图片\]\s*)+", message["text"]):
                rows = [{"title": "图片公示", "action": "", "notes": "需要看图或 OCR",
                         "condition": "", "quote": message["text"], "decision": "image",
                         "times": dict.fromkeys(TIMES), "link": None}]
            elif self.cloud and not admitted(message):
                rows = []
            else:
                context = {
                    "profile": self.model_profile, "now": message["at"],
                    "history": [e["message"] for e in snapshot["processed"].values()
                                if not self.all_groups or e["message"]["group"] == message["group"]][-2:],
                    "cards": [dict(id=card_id, **{field: card[field] for field in
                               ("title", "action", "notes", "condition", "decision", "times")})
                              for card_id, card in candidates.items()
                              if card["decision"] in ("todo", "review")][-12:],
                    "current": message,
                }
                aliases = {}
                if self.cloud:
                    context["current"] = project(message)
                    context["history"] = [project(item) for item in context["history"] if admitted(item)]
                    safe_cards = []
                    for card in context["cards"]:
                        original = candidates[card["id"]]
                        if not all(admitted(snapshot["processed"].get(e["id"], {}).get("message", {}))
                                   for e in original["evidence"]) or not original["evidence"]:
                            continue
                        alias = f"C{len(safe_cards)+1}"
                        aliases[alias] = card["id"]
                        safe_cards.append(dict(card, id=alias, notes=""))
                    context["cards"] = safe_cards
                raw, usage, actual_model = self.extract(context)
                if raw["finish_reason"] == "length":
                    raise ValueError("模型输出达到长度限制")
                rows = json.loads(raw["message"]["content"])["items"]
                if self.cloud:
                    for row in rows:
                        if row.get("link"):
                            row["link"] = aliases.get(row["link"], "unverified")
                        if row.get("candidates"):
                            row["candidates"] = [aliases.get(item, "unverified") for item in row["candidates"]]
                    validate_times(rows)
            with self.lock:
                state = read(self.path)
                before = set(state["cards"])
                if self.all_groups:
                    # Keep unrelated groups and title-only matches out of both inference and writing.
                    for row in rows:
                        if (row.get("link") and row["link"] not in candidates) or any(
                            item not in candidates for item in row.get("candidates", []) or []):
                            row.update(link=None, candidates=[], decision="review",
                                       notes="关联缺少明确依据，请确认对应事项。")
                results = apply(state, message, rows, auto_merge=not self.all_groups)
                if message.get("local_shares"):
                    share_key = message["id"] + ":share"
                    state["cards"].setdefault(share_key, dict(
                        title="待查看分享卡片", action="请在电脑或 QQ 查看分享卡片", notes="", condition="",
                        decision="review", times=dict.fromkeys(TIMES), manual_status=None,
                        evidence=[{"id": message["id"], "quote": "[分享卡片]"}],
                        local_share=True, message_at=message["at"]))
                    results.append({"card": share_key, "decision": "review"})
                stats = state["stats"]
                stats["processed"] += 1
                stats["created"] += len(set(state["cards"]) - before)
                for result in results:
                    if result["decision"] in ("todo", "review", "image"):
                        stats[result["decision"]] += 1
                state["processed"][key] = dict(message=message, source=("deepseek" if self.cloud else "local") if raw else "rule", model=actual_model if raw else None,
                                               raw=raw, usage=usage, results=results,
                                               attempts=entry["attempts"] + 1)
                del state["pending"][key]
                save(self.path, state)
            return True
        except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
            with self.lock:
                state = read(self.path)
                state["stats"]["failed"] += 1
                pending = state["pending"].pop(key)
                pending.update(attempts=pending["attempts"] + 1, error=type(error).__name__,
                               retry_after=datetime.now(LOCAL_ZONE).timestamp() + min(300, 10 * 2 ** min(pending["attempts"], 5)))
                state["pending"][key] = pending
                save(self.path, state)
            return False

    def work(self):
        while not self.stop.is_set():
            outcome = self.process_next()
            if outcome is not True:
                self.stop.wait(10 if outcome is False else 1)


def accept_history(pilot, messages, since, account=None):
    timestamps = [item["time"] for item in messages if isinstance(item, dict) and type(item.get("time")) is int]
    earliest = min(timestamps) if timestamps else None
    with pilot.lock:
        state = read(pilot.path)
        state["history_check"] = dict(checked_at=datetime.now(LOCAL_ZONE).isoformat(), since=since,
            returned=len(messages), earliest=earliest,
            possibly_incomplete=len(messages) >= 100 and (earliest is None or earliest > since))
        save(pilot.path, state)
    eligible = [item for item in messages if isinstance(item, dict)
                and type(item.get("time")) is int and item["time"] >= since
                and group_id(item.get("group_id")) == pilot.allowed]
    for item in sorted(eligible, key=lambda message: message["time"]):
        event = {"self_id": account, **item, "post_type": "message", "message_type": "group"}
        if str(event.get("user_id")) == str(event.get("self_id")):
            continue
        pilot.receive(event)
    return len(eligible)


def listen(pilot, port, token):
    import websocket

    while not pilot.stop.is_set():
        connection = None
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            connection = websocket.create_connection(f"ws://127.0.0.1:{port}", header=headers, timeout=5)
            print("NapCat 已连接。", flush=True)
            since = int((datetime.now(LOCAL_ZONE) - timedelta(hours=12)).timestamp())
            if os.environ.get("NAPCAT_CATCHUP") == "1":
                connection.send(json.dumps({"action": "get_login_info", "params": {}, "echo": "todo-login"}))
            account = None
            while not pilot.stop.is_set():
                try:
                    frame = connection.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not frame:
                    break
                try:
                    event = json.loads(frame)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(event, dict) and event.get("echo") == "todo-login":
                    data = event.get("data") or {}
                    account = data.get("user_id")
                    if not group_id(account):
                        print("历史补收未能确认登录账号。", flush=True)
                        continue
                    connection.send(json.dumps({
                        "action": "get_group_msg_history", "echo": "todo-history",
                        "params": {"group_id": pilot.allowed, "message_seq": "0", "count": 100,
                                   "disable_get_url": True},
                    }))
                elif isinstance(event, dict) and event.get("echo") == "todo-history":
                    messages = (event.get("data") or {}).get("messages")
                    if event.get("status") != "ok" or not isinstance(messages, list):
                        print("历史补收接口失败。", flush=True)
                    else:
                        count = accept_history(pilot, messages, since, account)
                        print(f"历史返回 {len(messages)} 条，窗口内候选 {count} 条；单次最多 100 条。", flush=True)
                else:
                    pilot.receive(event)
        except (OSError, websocket.WebSocketException) as error:
            print(f"NapCat 连接或接收失败（{type(error).__name__}），5 秒后重试。", flush=True)
        finally:
            if connection is not None:
                connection.close()
        pilot.stop.wait(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--group", type=group_id)
    parser.add_argument("--all-groups", action="store_true")
    parser.add_argument("--page-port", type=int, default=8765)
    parser.add_argument("--napcat-port", type=int, default=3001)
    parser.add_argument("--model-port", type=int, default=9931)
    parser.add_argument("--class-name", default="未知")
    args = parser.parse_args()
    if args.group and args.napcat_port == args.page_port:
        parser.error("页面和 NapCat 端口必须不同。")
    profile = {"班级": args.class_name, "年级": "未知", "分组名单中的姓名": "未知"}
    if args.group and args.all_groups:
        parser.error("不能同时指定单群和全部群模式。")
    pilot = Pilot(args.state, args.group, profile, args.model_port, args.all_groups)
    with ExitStack() as stack:
        server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", args.page_port), Handler))
        server.state_path, server.state_lock = pilot.path, pilot.lock
        server.extract_provider = "deepseek" if pilot.cloud else "local"
        server.extract_ready = not pilot.cloud or bool(os.environ.get("DEEPSEEK_API_KEY"))
        lan_address = os.environ.get("TODO_LAN_ADDRESS")
        if lan_address:
            address = IPv4Address(lan_address)
            network = IPv4Network(os.environ["TODO_LAN_SUBNET"])
            private = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            if address not in network or not any(network.subnet_of(IPv4Network(net)) for net in private):
                raise ValueError("局域网地址和子网必须匹配，且限于私有地址。")
            authorization = phone_auth(os.environ["TODO_PHONE_PASSWORD"])
            lan = stack.enter_context(ThreadingHTTPServer((str(address), 8877), LanHandler))
            lan.state_path, lan.state_lock = pilot.path, pilot.lock
            lan.origin, lan.lan_network, lan.phone_auth = f"http://{address}:8877", network, authorization
            Thread(target=lan.serve_forever, daemon=True).start()
            stack.callback(lan.shutdown)
            print(lan.origin, flush=True)
        serve_host = os.environ.get("TODO_SERVE_HOST")
        if serve_host:
            remote = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 8878), LanHandler))
            remote.state_path, remote.state_lock = pilot.path, pilot.lock
            remote.origin = f"https://{serve_host}"
            remote.lan_network = IPv4Network("127.0.0.1/32")
            remote.phone_auth = phone_auth(os.environ["TODO_PHONE_PASSWORD"])
            Thread(target=remote.serve_forever, daemon=True).start()
            stack.callback(remote.shutdown)
        Thread(target=pilot.work, daemon=True).start()
        if args.group or args.all_groups:
            from all_groups import listen_all
            Thread(target=listen_all if args.all_groups else listen, args=(pilot, args.napcat_port, os.environ.get("NAPCAT_ONEBOT_TOKEN")),
                   daemon=True).start()
        print(f"http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            pilot.stop.set()


if __name__ == "__main__":
    main()





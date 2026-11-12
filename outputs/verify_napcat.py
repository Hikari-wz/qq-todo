"""Exercise the pilot with synthetic OneBot events and a real local page write."""
import json
import sys
from http.server import HTTPServer
from ipaddress import IPv4Network
from pathlib import Path
from threading import Event, Thread
from urllib.request import Request, urlopen

from napcat_pilot import Pilot
from todo_server import Handler, LanHandler, phone_auth
from trial import TIMES, read, save

GROUP = "900001"
PROFILE = {"班级": "测试班", "年级": "未知", "分组名单中的姓名": "未知"}


def event(key, text, group=GROUP, kind="group"):
    return {"post_type": "message", "message_type": kind, "group_id": group, "self_id": 900000,
            "message_id": key, "time": 1794268800, "message": [{"type": "text", "data": {"text": text}}]}


def row(message, decision, link=None):
    title = "练习讨论" if decision == "review" else "练习资料"
    return dict(title=title, action=f"查看{title}", notes=message["text"], condition="",
                times=dict.fromkeys(TIMES), quote=message["text"], decision=decision, link=link)


def response(rows):
    return {"finish_reason": "stop", "message": {"content": json.dumps({"items": rows})}}, None, "mock"


def main(path):
    assert not path.exists(), "请使用新测试文件"
    pilot = Pilot(path, GROUP, PROFILE, 9931)
    pilot.receive({"post_type": "meta_event", "meta_event_type": "heartbeat", "raw_message": "心跳哨兵"})
    pilot.receive({"post_type": "notice", "raw_message": "通知哨兵"})
    first = event(1, "请查看练习资料。")
    optional = event(2, "自愿参加练习讨论。")
    pilot.receive(event(1, "其他群哨兵", "900002"))
    pilot.receive(event(1, "私聊哨兵", kind="private"))
    pilot.receive(first)
    pilot.receive(first)

    def timeout(_):
        raise TimeoutError

    pilot.extract = timeout
    assert pilot.process_next() is False
    initial_time = next(iter(read(path)["pending"].values()))["message"]["at"]
    pilot = Pilot(path, GROUP, PROFILE, 9931)
    pilot.receive(first)
    # Verify failure remains queued; explicitly advance its retry clock for this test.
    retry_state = read(path)
    assert next(iter(retry_state["pending"].values()))["retry_after"] > 0
    for pending in retry_state["pending"].values():
        pending["retry_after"] = 0
    save(path, retry_state)
    pilot.extract = lambda ctx: response([row(ctx["current"], "todo")])
    assert pilot.process_next()
    assert next(iter(read(path)["processed"].values()))["message"]["at"] == initial_time
    pilot.receive(first)
    pilot.receive(optional)
    pilot.extract = lambda ctx: response([row(ctx["current"], "review")])
    assert pilot.process_next()
    first_card, optional_card = read(path)["cards"]
    pilot.receive(event(3, "练习资料补充：入口改到测试平台。"))
    pilot.receive(event(4, "讨论补充：地点改为测试室。"))
    started, resume = Event(), Event()

    def linked(ctx):
        started.set()
        assert resume.wait(10)
        is_optional = "讨论" in ctx["current"]["text"]
        return response([row(ctx["current"], "review" if is_optional else "todo",
                             optional_card if is_optional else first_card)])

    pilot.extract = linked
    with HTTPServer(("127.0.0.1", 0), Handler) as server, HTTPServer(("127.0.0.1", 0), LanHandler) as lan:
        server.state_path, server.state_lock = path, pilot.lock
        lan.state_path, lan.state_lock = path, pilot.lock
        lan.origin = f"http://127.0.0.1:{lan.server_port}"
        lan.lan_network = IPv4Network("127.0.0.0/8")
        lan.phone_auth = phone_auth("synthetic-test-password")
        thread = Thread(target=server.serve_forever, daemon=True)
        lan_thread = Thread(target=lan.serve_forever, daemon=True)
        thread.start()
        lan_thread.start()
        worker = Thread(target=pilot.process_next)
        worker.start()
        try:
            assert started.wait(10)
            origin = f"http://127.0.0.1:{server.server_port}"
            for key, status in [(first_card, "done"), (optional_card, "dismissed")]:
                req = Request(lan.origin + "/api/status", json.dumps({"id": key, "status": status}).encode(),
                              {"Origin": lan.origin, "Content-Type": "application/json",
                               "Authorization": lan.phone_auth.decode("ascii")})
                with urlopen(req, timeout=5) as result:
                    assert result.status == 200
            with urlopen(origin + "/api/cards", timeout=5) as result:
                assert {card["manual_status"] for card in json.load(result)} == {"done", "dismissed"}
            with urlopen(Request(lan.origin + "/api/cards", headers={
                "Authorization": lan.phone_auth.decode("ascii")
            }), timeout=5) as result:
                phone_cards = json.load(result)
                assert len(phone_cards) == 2
                assert all(not card["notes"] for card in phone_cards)
                assert all(set(source) == {"group", "at", "text"}
                           for card in phone_cards for source in card["evidence"])
        finally:
            resume.set()
            worker.join(10)
            server.shutdown()
            lan.shutdown()
            thread.join(5)
            lan_thread.join(5)
    assert pilot.process_next()
    image = event(5, "")
    image["message"] = [{"type": "image", "data": {"url": "https://example.invalid/secret"}}]
    pilot.receive(image)
    pilot.extract = lambda _: (_ for _ in ()).throw(AssertionError("图片不应请求模型"))
    assert pilot.process_next()
    final = read(path)
    assert {k: final["filter_counts"][k] for k in ("heartbeat", "non_message", "private", "other_group")} == {
        "heartbeat": 1, "non_message": 1, "private": 1, "other_group": 1}
    assert final["stats"]["accepted"] == 5 and final["stats"]["duplicate"] == 3
    assert len(final["cards"]) == 3 and not final["pending"]
    for key, status in [(first_card, "done"), (optional_card, "dismissed")]:
        assert final["cards"][key]["manual_status"] == status
        assert len(final["cards"][key]["evidence"]) == 2
    assert not any(value in path.read_text(encoding="utf-8") for value in ("哨兵", "example.invalid"))
    print(final["stats"])
    print(final["filter_counts"])


if __name__ == "__main__":
    main(Path(sys.argv[1]))


"""Tests fixture expectations separately from actual model inference."""
import copy
import subprocess
import sys
from tempfile import TemporaryDirectory
from pathlib import Path

from trial import ROOT, apply, read


def main():
    samples = read(ROOT / "samples.json")
    mock = read(ROOT / "mock.json")
    state = {"cards": {}}
    for i, (message, rows) in enumerate(zip(samples, mock, strict=True)):
        results = apply(state, message, rows)
        if i == 1:
            state["cards"]["m2:1"]["manual_status"] = "done"
        if i == 6:
            assert len(results) == 2
            assert all(row["decision"] == "skip" for row in results)
    cards = state["cards"]
    assert set(cards) == {"m1:1", "m2:1", "m4:1", "m5:1", "m5:2", "m8:1"}
    assert cards["m1:1"]["times"] == dict(deadline=None, arrival="2026-11-11T12:15",
                                         start="2026-11-11T12:30", end="2026-11-11T13:30")
    assert cards["m2:1"]["manual_status"] == "done"
    assert cards["m2:1"]["times"]["deadline"] == "2026-11-13"
    assert cards["m2:1"]["condition"] == "未完成才做"
    assert [e["id"] for e in cards["m2:1"]["evidence"]] == ["m2", "m3"]
    assert cards["m4:1"]["decision"] == "review"
    assert cards["m4:1"]["times"]["deadline"] is None
    assert [e["id"] for e in cards["m5:1"]["evidence"]] == ["m5", "m6"]
    assert "意向表" in cards["m5:1"]["notes"]
    assert cards["m5:2"]["decision"] == "review"
    assert cards["m8:1"]["decision"] == "image"

    self_notice = read(ROOT / "round2-samples.json")["class-self"][0]
    extra_condition = dict(mock[1][0], title="补交实验报告", action="补交实验报告",
                           condition="未完成该事项才做", quote="另请三班同学补交实验报告。",
                           times=dict.fromkeys(mock[1][0]["times"]))
    extra_state = {"cards": {}}
    result = apply(extra_state, self_notice, [extra_condition])[0]
    assert result["decision"] == "todo" and result["times"]["deadline"] is None
    assert result["condition"] == extra_state["cards"][result["card"]]["condition"] == ""
    assert extra_condition["condition"] == "未完成该事项才做"

    follow_up = dict(samples[2], id="condition-follow-up", text="期中评教入口改为教学平台。")
    follow_up_row = dict(mock[2][0], quote=follow_up["text"], condition="")
    assert apply(state, follow_up, [follow_up_row])[0]["condition"] == "未完成才做"
    assert cards["m2:1"]["manual_status"] == "done"
    known = copy.deepcopy(mock[8][0])
    known.update(kind="roster", decision="skip", action="", notes="样例甲已在名单")
    for status in ("done", "dismissed"):
        cards["m4:1"]["manual_status"] = status
        state["profile"] = {"分组名单中的姓名": "未知"}
        apply(state, samples[8], [known])
        assert cards["m4:1"]["decision"] == "review"
        assert cards["m4:1"]["manual_status"] == status
        state["profile"]["分组名单中的姓名"] = "样例甲"
        apply(state, samples[8], [known])
        assert cards["m4:1"]["decision"] == "skip"
        assert cards["m4:1"]["manual_status"] == status
        assert "样例甲" in cards["m4:1"]["evidence"][-1]["quote"]
        assert "不用再交" in cards["m4:1"]["evidence"][-1]["quote"]

    mention = dict(samples[8], id="mention",
                   text="请样例甲帮忙转发分组名单，名单见附件。已在分组名单上的同学不用再交。")
    apply(state, mention, [dict(known, quote=mention["text"])])
    assert cards["m4:1"]["decision"] == "review"
    confirmed = dict(known, decision="review", quote="其他组见附件")
    apply(state, samples[8], [confirmed])
    assert cards["m4:1"]["decision"] == "skip"
    assert cards["m4:1"]["times"]["deadline"] is None

    ambiguous = dict(mock[5][0], candidates=["m5:1", "m5:2"])
    before_cards = copy.deepcopy(cards)
    result = apply(state, dict(samples[5], id="ambiguous"), [ambiguous])[0]
    assert result["decision"] == "review" and result["link"] is None
    assert all(cards[key] == card for key, card in before_cards.items())

    invitation, unresolved = read(ROOT / "round2-samples.json")["ambiguous"]
    for link in (None, "a1:1"):
        clubs = {"cards": {}}
        apply(clubs, invitation, [
            dict(row, title=title, quote=invitation["text"])
            for row, title in zip(mock[4], ("数据建模社报名和宣讲", "机器人社报名和宣讲"), strict=True)
        ])
        original_clubs = copy.deepcopy(clubs["cards"])
        row = dict(ambiguous, link=link, candidates=["a1:1"],
                   quote="请有兴趣的人填写意向表")
        result = apply(clubs, unresolved, [row])[0]
        assert result["decision"] == "review" and result["link"] is None
        assert result["card"] == "a2:1"
        assert len(clubs["cards"]) == 3
        assert all(clubs["cards"][key] == card for key, card in original_clubs.items())

    supplement = dict(mock[5][0], link=None, candidates=["m5:1"], action="填写表单")
    result = apply(state, dict(samples[5], id="unique"), [supplement])[0]
    assert result["card"] == "m5:1"
    assert cards["m5:1"]["action"] == before_cards["m5:1"]["action"]
    assert cards["m5:1"]["evidence"][-1]["id"] == "unique"

    duplicate_time = dict(mock[0][0], times=dict(mock[0][0]["times"]))
    duplicate_time["times"]["deadline"] = duplicate_time["times"]["arrival"]
    assert apply({"cards": {}}, samples[0], [duplicate_time])[0]["times"]["deadline"] is None

    for changes in ({"quote": "原文里没有这句话"},
                    {"times": dict(mock[0][0]["times"], arrival="2026-11-31T12:15")},
                    {"link": "missing:1"}):
        invalid = dict(mock[0][0], **changes)
        empty = {"cards": {}}
        try:
            apply(empty, samples[0], [invalid])
        except ValueError:
            assert not empty["cards"]
        else:
            raise AssertionError(f"accepted invalid item: {changes}")

    with TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        command = [sys.executable, str(ROOT / "trial.py"), "--mode", "mock", "--state", str(path)]
        subprocess.run(command, check=True)
        before = path.read_bytes()
        subprocess.run(command, check=True)
        assert before == path.read_bytes()
    print("PASS: mock, manual status, known identity, replay")


if __name__ == "__main__":
    main()

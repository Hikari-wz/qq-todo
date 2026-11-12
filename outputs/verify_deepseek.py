"""Exercise the DeepSeek request and replay without contacting the API."""
import io
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

import trial


def main():
    secret = "fake-deepseek-token-for-test"
    text = trial.read(trial.ROOT / "round2-samples.json")["class-mixed"][0]["text"]
    assert "三班同学必须提交课程反思" in text
    rows = [
        dict(title=title, decision=decision, quote=quote, action=action,
             notes="", condition="", times=dict.fromkeys(trial.TIMES), link=None)
        for title, decision, quote, action in [
            ("品牌讲座", "skip", "欢迎自愿参加商业品牌新品讲座", ""),
            ("二班报告", "skip", "二班同学补交实验报告", ""),
            ("课程反思", "todo", "三班同学必须提交课程反思", "提交课程反思"),
        ]
    ]
    usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    with TemporaryDirectory() as directory:
        state = Path(directory) / "state.json"
        argv = ["trial.py", "--provider", "deepseek", "--model", "test-model",
                "--case", "class-mixed", "--state", str(state)]
        with patch.object(sys, "argv", argv), patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}), (
            patch.object(trial, "build_opener")
        ) as opener:
            endpoint = opener.return_value.open
            endpoint.side_effect = HTTPError("https://api.deepseek.com/chat/completions",
                                             429, secret, {}, None)
            try:
                trial.main()
            except SystemExit:
                pass
            else:
                raise AssertionError("HTTP 429 should stop before committing the message")
            failure = state.with_suffix(".error.json")
            assert trial.read(failure)["error"] == "HTTP 429"
            assert secret not in failure.read_text(encoding="utf-8")
            assert not state.exists()

            endpoint.side_effect = None
            choice = {"finish_reason": "stop", "message": {
                "content": json.dumps({"items": rows}, ensure_ascii=False)}}
            endpoint.return_value = io.BytesIO(json.dumps({
                "choices": [choice], "usage": usage, "model": "test-model",
            }).encode())
            trial.main()
            request = endpoint.call_args.args[0]
            assert request.full_url == "https://api.deepseek.com/chat/completions"
            assert request.get_header("Authorization") == f"Bearer {secret}"
            body = json.loads(request.data)
            assert body["model"] == "test-model" and secret not in request.data.decode()
            assert body["response_format"] == {"type": "json_object"}
            assert "json" in body["messages"][0]["content"].lower()
            assert json.loads(body["messages"][-1]["content"])["now"] == "2026-11-10 10:00"
            output = trial.read(state)
            entry, = output["processed"].values()
            assert entry["raw"] == choice and entry["requests"][0]["usage"] == usage
            assert entry["requests"][0]["elapsed_ms"] >= 0
            assert [card["title"] for card in output["cards"].values()] == ["课程反思"]
            assert secret not in state.read_text(encoding="utf-8")
            trial.main()
            assert endpoint.call_count == 2
    print("PASS: DeepSeek request, telemetry, HTTP 429 resume, mixed notice and replay")


if __name__ == "__main__":
    main()

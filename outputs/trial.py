import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from time import perf_counter, sleep
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
TIMES = ("deadline", "arrival", "start", "end")
TEXT = ("title", "action", "notes", "condition", "quote")
DECISIONS = ("todo", "review", "skip", "image")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(temp, path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            sleep(0.05 * (attempt + 1))


def completion_body(prompt, context, cloud, model):
    body = {
        "model": model, "temperature": 0, "max_tokens": 1200 if cloud else 650,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
    }
    if cloud:
        body["thinking"] = {"type": "disabled"}
    else:
        body.update(seed=42, chat_template_kwargs={"enable_thinking": False})
        body["messages"][-1]["content"] += "\n/no_think"
    return body


def infer(port, prompt, context, provider="local", model="local", api_key=None):
    cloud = provider == "deepseek"
    body = completion_body(prompt, context, cloud, model)
    headers = {"Content-Type": "application/json"}
    if cloud:
        url = "https://api.deepseek.com/chat/completions"
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
    request = Request(url, json.dumps(body).encode(), headers)
    with build_opener(ProxyHandler({})).open(request, timeout=240) as response:
        payload = response.read().decode("utf-8")
    if api_key:
        payload = payload.replace(api_key, "[REDACTED]")
    response = json.loads(payload)
    return response["choices"][0], response.get("usage"), response.get("model")


def error_detail(error, api_key):
    if not isinstance(error, HTTPError):
        detail = str(error)
    else:
        detail = f"HTTP {error.code}"
        if error.fp is not None:
            try:
                message = json.loads(error.read(16384))["error"]["message"]
                if isinstance(message, str):
                    detail += " " + message
            except (ValueError, KeyError, TypeError, OSError):
                pass
    if api_key:
        detail = detail.replace(api_key, "[REDACTED]")
        detail = re.sub(r"(?i)\bsk-[a-z0-9_-]+", "[REDACTED]", detail)
    return detail[:1000]


GROUP_LABEL = r"第[一二三四五六七八九十百0-9]+组|[A-Za-z0-9一二三四五六七八九十]+组"
MEMBER_TOKEN = r"[\w·]{1,24}"
MEMBER_LIST = rf"{MEMBER_TOKEN}(?:\s*[、,，]\s*{MEMBER_TOKEN})*"
ROSTER_LINES = (
    re.compile(rf"^(?:[^：:]*?(?:名单|分组|编组)(?:已公布|结果|如下)?[：:]\s*)?(?P<members>{MEMBER_LIST})\s*(?:均|都)?(?:在|已加入|已分入|被分到)(?:{GROUP_LABEL})$"),
    re.compile(rf"^(?:{GROUP_LABEL})\s*(?:成员|名单)?[：:]\s*(?P<members>{MEMBER_LIST})$"),
)
ROSTER_EXEMPTION = re.compile(
    r"(?:已在.{0,16}名单上|已入组|已分组|已加入.{0,12}组)"
    r"(?:的(?:同学|成员|人员|人))?\s*(?:[，,]\s*)?(?:无需|不用|不必|免于|免)(?:再|重复)?(?:提交|交|报)"
)
UNRESOLVED_TARGET = re.compile(
    r"(?:具体是哪个|究竟是哪个|具体对应哪|对应对象|关联对象|指向|归属)"
    r"[^。！？；\n]{0,24}(?:稍后确认|另行确认|待确认|待定|尚未确定|不明确)"
)
COMPLETION_CONDITION = re.compile(r"未(?:完成|提交|办理|参加|做过)|(?:尚未|还没)(?:完成|提交|办理)|已完成.{0,12}(?:免|无需|不用|不必)")
COMPLETION_EVIDENCE = re.compile(
    r"(?:尚未|还没|未)(?:完成|提交|办理|参加|做过)[^。！？；\n]{0,24}(?:才|请|须|需|必须|补)"
    r"|(?:已|已经)(?:完成|提交|办理|参加|做过)[^。！？；\n]{0,18}(?:无需|不用|不必|免)"
)
COMPLETION_OVERRIDE = re.compile(
    r"(?:已|已经)(?:完成|提交|办理|参加|做过)[^。！？；\n]{0,18}(?:也|仍|同样)(?:必须|需要|要|须|需)"
)


def roster_proof(text, name):
    if not name or name == "未知" or not ROSTER_EXEMPTION.search(text):
        return False
    for clause in re.split(r"[。！；;\r\n]+", text):
        if re.search(r"[？?]|是否|拟定|暂定|待确认|待核对|可能", clause):
            continue
        for pattern in ROSTER_LINES:
            match = pattern.fullmatch(clause.strip())
            if match and name in re.split(r"\s*[、,，]\s*", match["members"]):
                return True
    return False


def apply(state, message, rows, auto_merge=True):
    if not isinstance(rows, list):
        raise ValueError("items 必须是数组")
    cards = state["cards"]
    existing = set(cards)
    explicit_links = {row["link"] for row in rows
                      if isinstance(row, dict) and isinstance(row.get("link"), str)
                      and row["link"] in existing}
    linked = set()
    results = []
    for index, row in enumerate(rows, 1):
        row = dict(row)
        if any(not isinstance(row.get(field), str) for field in TEXT):
            raise ValueError("文本字段格式错误")
        if not row["title"].strip() or not row["quote"].strip() or row["quote"] not in message["text"]:
            raise ValueError("标题为空或引用不在当前消息中")
        decision = row["decision"]
        if decision not in DECISIONS or (decision == "todo" and not row["action"].strip()):
            raise ValueError("分类无效或必做缺少行动")
        times = row["times"]
        if set(times) != set(TIMES):
            raise ValueError("时间字段不完整")
        for value in times.values():
            if value is not None:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?", value):
                    raise ValueError(f"时间格式错误 {value!r}")
                datetime.fromisoformat(value)
        if times["deadline"] is not None and times["deadline"] == times["arrival"]:
            deadline_quote = row.get("deadline_quote")
            # A shared timestamp needs independent evidence for the submission deadline.
            if not isinstance(deadline_quote, str) or not deadline_quote.strip() or (
                deadline_quote not in message["text"] or deadline_quote == row["quote"]
            ):
                times = dict(times, deadline=None)
                row["times"] = times
        target = row.get("link")
        if target is not None and (not isinstance(target, str) or target not in existing):
            raise ValueError(f"关联卡片不存在 {target!r}")
        candidates = row.get("candidates")
        if candidates is None:
            candidates = []
        if not isinstance(candidates, list) or any(
            not isinstance(candidate, str) or candidate not in existing for candidate in candidates
        ):
            raise ValueError("候选关联 ID 无效")
        scope = message["text"] if len(rows) == 1 else row["quote"]
        ambiguous = (len(set(candidates)) > 1 or row.get("association") == "unresolved"
                     or (bool(target or candidates) and bool(UNRESOLVED_TARGET.search(scope))))
        if ambiguous:
            target = None
            row.update(link=None, decision="review")
        elif target is None and len(candidates) == 1:
            target = candidates[0]
            if target in explicit_links:
                continue
            previous = cards[target]
            row.update(link=target, title=previous["title"])
            if row.get("kind") != "roster":
                row.update(action=previous["action"],
                           notes="\n".join(dict.fromkeys(filter(None, (previous["notes"], row["notes"])))))
                times = {field: times[field] or previous["times"][field] for field in TIMES}
                row["times"] = times
        if row.get("kind") == "roster" and not ambiguous:
            name = state.get("profile", {}).get("分组名单中的姓名")
            if target and roster_proof(message["text"], name):
                row.update(decision="skip", action="", notes="原文确认本人已入组，按通知免于再次提交。",
                           condition="", quote=message["text"])
            elif row["decision"] == "skip":
                row.update(decision="review", action="核对本人是否已入组",
                           notes="当前引用无法确认本人已入组，需要核对姓名与完整名单。", condition="")
        if row.get("kind") == "roster" and target and times["deadline"] is not None:
            if times["deadline"] == cards[target]["times"]["deadline"]:
                times = dict(times, deadline=None)
                row["times"] = times
        decision = row["decision"]
        if auto_merge and target is None and not ambiguous and decision in ("todo", "review"):
            # 精确标题和行动均相同时，兜底合并复发通知。
            matches = [key for key in existing if
                       (cards[key]["title"], cards[key]["action"]) == (row["title"], row["action"])]
            if len(matches) == 1:
                target = matches[0]
        if target in linked:
            raise ValueError(f"同一消息多次更新同一张卡 {target}")
        if target is not None:
            linked.add(target)
        key = target or f'{message["id"]}:{index}'
        if target is not None or decision in ("todo", "review", "image"):
            previous = cards.get(key, {})
            if decision in ("todo", "review") and row.get("kind") != "roster":
                prior_condition = previous.get("condition", "")
                if COMPLETION_CONDITION.search(row["condition"]) and not COMPLETION_EVIDENCE.search(row["quote"]):
                    row["condition"] = ""
                if not row["condition"] and not (
                    COMPLETION_CONDITION.search(prior_condition) and COMPLETION_OVERRIDE.search(row["quote"])
                ):
                    row["condition"] = prior_condition
            cards[key] = {
                **{field: row[field] for field in TEXT if field != "quote"},
                "decision": decision, "times": times,
                "evidence": previous.get("evidence", []) + [{"id": message["id"], "quote": row["quote"]}],
                "manual_status": previous.get("manual_status"),
            }
        results.append({"card": key if key in cards else None, **row})
    return results


def set_manual_status(state, key, status):
    if key not in state["cards"] or status not in ("open", "done", "dismissed", "clear"):
        raise ValueError("卡片不存在或状态无效")
    state["cards"][key]["manual_status"] = None if status == "clear" else status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("mock", "local"), default="local",
                        help="local 表示执行推理，供应商由 --provider 选择")
    parser.add_argument("--provider", choices=("local", "deepseek"), default="local")
    parser.add_argument("--model", help="DeepSeek 模型 ID，默认 deepseek-flash")
    parser.add_argument("--case", choices=("original", "roster", "roster-known", "class-other",
                                          "class-self", "form", "ambiguous", "image", "class-mixed"),
                        default="original")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--samples", type=Path, default=ROOT / "samples.json")
    parser.add_argument("--port", type=int, default=18089)
    parser.add_argument("--name", default="未知")
    parser.add_argument("--set-status", nargs=2, metavar=("CARD", "STATUS"))
    args = parser.parse_args()
    cloud = args.provider == "deepseek"
    if cloud and (args.mode == "mock" or args.samples.resolve() != ROOT / "samples.json" or args.name != "未知"):
        parser.error("DeepSeek 仅允许使用内置脱敏回放，请用 --case 选择样例")
    if args.model and not cloud:
        parser.error("--model 仅用于 DeepSeek")
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip() if cloud else None
    model = (args.model or "deepseek-flash") if cloud else "local"
    name = "样例甲" if args.case == "roster-known" else args.name
    profile = {"班级": "2603班（即三班）", "年级": "新生", "分组名单中的姓名": name}
    state = read(args.state) if args.state.exists() else {
        "mode": args.mode, "profile": profile, "cards": {}, "processed": {},
        "provider": args.provider, "model": model, "case": args.case,
    }
    if (state["mode"], state["profile"], state.get("provider", "local"),
        state.get("model", "local"), state.get("case", "original")) != (
        args.mode, profile, args.provider, model, args.case
    ):
        parser.error("请为不同供应商、模型、样例或身份使用独立状态文件")
    if args.set_status:
        key, status = args.set_status
        try:
            set_manual_status(state, key, status)
        except ValueError as error:
            parser.error(str(error))
        save(args.state, state)
        return
    if cloud and not api_key:
        parser.error("缺少 DEEPSEEK_API_KEY")
    if args.case == "original":
        samples = read(args.samples)
    else:
        case = "roster" if args.case == "roster-known" else args.case
        samples = read(ROOT / "round2-samples.json")[case]
    if len({m["id"] for m in samples}) != len(samples):
        parser.error("消息 ID 重复")
    processed = state["processed"]
    for message in samples:
        prior = processed.get(message["id"])
        if prior and prior["message"] != message:
            parser.error(f'消息 {message["id"]} 已变更，请用新的状态文件重跑')
    prompt = (ROOT / "prompt.txt").read_text(encoding="utf-8")
    if args.mode == "mock":
        if args.samples.resolve() != (ROOT / "samples.json").resolve() or args.name != "未知" or args.case != "original":
            parser.error("模拟输出仅对应默认样例和未知身份")
        mock = read(ROOT / "mock.json")
    for index, message in enumerate(samples):
        key = message["id"]
        if key in processed:
            continue
        print(f"{args.provider if args.mode == 'local' else args.mode} {key}", flush=True)
        context = {"profile": profile, "now": message["at"],
                   "history": [p["message"] for p in list(processed.values())[-2:]],
                   "cards": [dict(id=card_id, **{field: card[field] for field in
                              ("title", "action", "notes", "condition", "decision", "times")})
                             for card_id, card in state["cards"].items()
                             if card["decision"] in ("todo", "review")],
                   "current": message}
        raw = None
        retries = []
        requests = []
        try:
            if re.fullmatch(r"(?:[^。！？；\r\n:：\[\]]{1,60}[:：]\s*)?\[图片\]",
                            message["text"].strip()):
                rows = [{"title": "图片公示", "action": "", "notes": "需要看图或 OCR",
                         "condition": "", "quote": message["text"].strip(), "decision": "image",
                         "times": dict.fromkeys(TIMES), "link": None}]
                source = "rule"
            elif args.mode == "mock":
                rows, source = mock[index], "mock"
            else:
                source = "model"
                retry_prompt = prompt
                for attempt in range(2):
                    started = perf_counter()
                    metrics = {"elapsed_ms": None, "usage": None, "model": None}
                    requests.append(metrics)
                    try:
                        try:
                            raw, metrics["usage"], metrics["model"] = infer(
                                args.port, retry_prompt, context, args.provider, model, api_key)
                        finally:
                            metrics["elapsed_ms"] = round((perf_counter() - started) * 1000)
                        if raw["finish_reason"] == "length":
                            raise ValueError("模型输出达到长度限制")
                        rows = json.loads(raw["message"]["content"])["items"]
                        staged = dict(state, cards=dict(state["cards"]))
                        results = apply(staged, message, rows)
                    except (HTTPError, ValueError, KeyError, TypeError, AttributeError) as error:
                        if isinstance(error, HTTPError) and error.code != 400:
                            raise
                        detail = error_detail(error, api_key)
                        retries.append({"error": detail, "raw": raw})
                        if attempt:
                            raise ValueError(detail) from error
                        raw = None
                        context["history"] = []
                        retry_prompt = prompt + "\n上次输出未通过校验。只重做当前消息，严禁复述无当前原文依据的旧卡。"
                        continue
                    state["cards"] = staged["cards"]
                    break
            if source != "model":
                results = apply(state, message, rows)
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
            failure = args.state.with_suffix(".error.json")
            detail = error_detail(error, api_key)
            save(failure, {"message": message, "error": detail, "raw": raw, "retries": retries,
                           "requests": requests})
            raise SystemExit(f"{key} 处理失败，详情见 {failure}") from error
        processed[key] = {"message": message, "source": source, "raw": raw,
                          "retries": retries, "requests": requests, "results": results}
        save(args.state, state)
    print(f"已处理 {len(processed)} 条消息，保存 {len(state['cards'])} 张卡。")


if __name__ == "__main__":
    main()

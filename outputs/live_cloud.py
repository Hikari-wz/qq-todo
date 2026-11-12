"""Local admission and payload projection for the live cloud extractor."""
import re

from group_filters import MUTED_GROUPS
SCHOOL = re.compile(
    r"同学们好|各位同学|班会|团日|选课|评教|签到|补考|教务|"
    r"讲座|创新创业学分|校园定向越野|补课|调课|随堂|各班委|"
    r"(?:记得|正常|按时|安排).{0,12}上课|上课.{0,12}(?:教室|调整|改为)|"
    r"入场完毕|参加的同学|作业.{0,12}(公布|截止|提交|打回)|"
    r"(?:调整|改到|改为|定于).{0,18}(?:上午|下午|教室|\d{1,2}[：:]\d{2})|"
    r"(?:教室|地点|时间).{0,12}(?:另行通知|改到|改为|调整|定为)|"
    r"请.{0,18}(?:提交|准备|提供|填写|到场|报名|携带|交齐)"
)

def admitted(message):
    gid = message.get("id", "").split(":")
    return (len(gid) == 4 and gid[0] == "qq" and gid[2] not in MUTED_GROUPS
            and not message.get("local_shares") and bool(SCHOOL.search(message["text"])))

def project(message):
    return {key: message[key] for key in ("at", "text")}

def validate_times(rows):
    for row in rows:
        quote = row.get("quote", "")
        times = row.get("times", {})
        if not isinstance(quote, str) or not isinstance(times, dict):
            continue
        has_clock = re.search(r"\d{1,2}\s*[:：]\s*\d{2}|[零一二三四五六七八九十两\d]+\s*(?:点|时)(?!间|候|前后)", quote)
        for key, value in times.items():
            if isinstance(value, str) and "T" in value:
                midnight = value.endswith("T00:00")
                explicit_midnight = re.search(r"00[:：]00|午夜|零点|凌晨[零0]点|晚上十二点|24[:：]00", quote)
                if (midnight and not explicit_midnight) or (not has_clock and not explicit_midnight):
                    times[key] = value.split("T", 1)[0]
        if times.get("deadline") and not (
            re.search(r"提交|交齐|补交|交作业|交报告|交.{0,8}(?:名单|材料|证明|作文)|填写|报名|完成|评教|提供|私信|上报", quote)
            and re.search(r"截止|之前|以前|前|截至|最晚|限期", quote)
        ):
            times["deadline"] = None
        if times.get("arrival") and not re.search(r"到场|到达|入场|集合|报到", quote):
            times["arrival"] = None
        if times.get("end") and not re.search(r"结束|散会|至|到\s*\d|[-–—~～]", quote):
            times["end"] = None



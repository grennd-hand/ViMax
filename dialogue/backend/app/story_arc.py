"""A source-bounded sequence of scene events for long fictional group chats."""

from __future__ import annotations

import re

from .core import BRACKET_RE, URL_RE, source_literals


_FALLBACK_EVENTS = [
    "先入群的人说出自己想试的原因，另一个人只追问他最在意的一点",
    "一位愿意行动的人先试已公布的第一步，随后说出一个具体感受",
    "有人把自己对步骤的理解说出来，已试过的人纠正其中一个误会",
    "一位成员按自己的节奏往下做，另一位选择先听他的实际体验",
    "有人回到群里，接住刚才一位成员说过的具体经历",
    "一位成员发现自己先前的话容易让人误解，直接澄清原意",
    "两位成员对花多少时间在这件事上有不同选择，各自说出原因",
    "一位成员在已知步骤中犯了可自行纠正的小错，并说明改正后的状态",
    "后来的人问先试者一个尚未讨论过的亲身体验细节",
    "先试者回答后，提问者按自己的人格作出不同的决定",
    "有人对另一位成员的说法提出具体异议，两人就那一点说清楚",
    "一位平时少说话的人记起前面某人的经历，说明它怎样影响了自己",
    "有人发现自己与另一位成员做法不同，比较的是个人取舍而非新规则",
    "一位成员检查自己已经公开做过的动作，发现有一句进度说得不准确并更正",
    "有人向先行者请教一个操作中的小习惯，对方只谈自己的做法",
    "两位成员就是否继续投入时间给出不同但都明确的个人选择",
    "有人直接回应前面被忽略的一句个人感受，不重新汇报完整流程",
    "准备结束的人交代自己的实际结果，尚未完成的人作出自己的决定",
    "坐席针对群里仍未说清的一个问题作简短回答，不逐人复述进度",
    "最后各人只补充有变化的决定，由坐席收束已完成与未完成的状态",
]


def fallback_story_arc(target: int) -> list[dict]:
    count = min(20, max(6 if target < 80 else 8, round(target / 8)))
    events = _FALLBACK_EVENTS[:count - 1] + [_FALLBACK_EVENTS[-1]]
    return [{"event": event, "characters": [],
             "kind": "closure" if index == len(events) - 1 else "action" if index < max(3, count // 3) else "interaction"}
            for index, event in enumerate(events)]


def normalize_story_topics(raw: object, source: dict) -> list[dict]:
    """Keep distinct topics with at least one real source row as evidence."""
    items = raw.get("topics") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    valid_rows = {item.get("source_row") for item in source.get("messages", [])}
    topics = []
    seen = set()
    for item in items[:24]:
        if not isinstance(item, dict):
            continue
        rows = item.get("source_rows", item.get("原文行号", []))
        rows = [row for row in rows if row in valid_rows][:8] if isinstance(rows, list) else []
        point = " ".join(str(item.get("point", item.get("原话要点", ""))).split())[:180]
        tension = " ".join(str(item.get("tension", item.get("虚构群聊分歧或个人选择", ""))).split())[:180]
        key = re.sub(r"\W", "", point).lower()
        if not rows or len(point) < 8 or len(tension) < 8 or key in seen:
            continue
        seen.add(key)
        topics.append({"id": len(topics), "source_rows": rows, "point": point, "tension": tension})
        if len(topics) == 18:
            break
    return topics


def normalize_story_arc(raw: object, source: dict, roles: list[str], target: int, *, topic_count: int = 0,
                        continuation: bool = False) -> list[dict]:
    """Keep short, distinct scene cues; reject foreign literals and outcome claims."""
    items = raw.get("beats") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        items = []
    allowed = set(source_literals(source))
    source_text = "\n".join(str(item.get("content") or "") for item in source.get("messages", []))
    accepted: list[dict] = []
    seen: set[str] = set()
    wanted = min(30, max(6 if target < 80 else 8, round(target / 5)))
    candidates = items[:wanted - 1] + [items[-1]] if len(items) > wanted else items
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        event = " ".join(str(item.get("event") or "").split())
        if not event and isinstance(item.get("dialogue"), list):
            # Some compatible models return proposed lines instead of a synopsis.
            # They remain plot proposals, never committed dialogue or evidence.
            lines = item["dialogue"]
            if 1 <= len(lines) <= 3 and all(isinstance(line, str) for line in lines):
                event = "可引出的个人交流：" + " / ".join(" ".join(line.split()) for line in lines)
        if not 12 <= len(event) <= 600:
            continue
        if any(detail in event and detail not in source_text for detail in
               ("手机端", "弹窗", "按钮", "截图样例", "点赞位置", "出现点赞结果", "页面加载", "无法打开")):
            continue
        literals = set(URL_RE.findall(event) + BRACKET_RE.findall(event))
        if literals - allowed or re.search(r"已到账|到账了|经理已确认|官方已认证|保证发放|必须付款", event):
            continue
        kind = item.get("kind")
        if kind not in {"action", "interaction", "closure"}:
            kind = "closure" if index == len(candidates) - 1 else "action" if index < max(3, wanted // 3) else "interaction"
        key = re.sub(r"\W", "", event).lower()
        if key in seen:
            continue
        seen.add(key)
        characters = item.get("characters")
        people = [name for name in characters if name in roles][:3] if isinstance(characters, list) else []
        topic_id = item.get("topic_id")
        source_rows = item.get("source_rows")
        beat = {"event": event, "characters": people, "kind": kind}
        if isinstance(topic_id, int) and topic_id >= 0:
            beat["topic_id"] = topic_id
        if isinstance(source_rows, list):
            valid_rows = {message.get("source_row") for message in source.get("messages", [])}
            beat["source_rows"] = [row for row in source_rows if row in valid_rows][:6]
        accepted.append(beat)
        if len(accepted) >= wanted:
            break
    # Continuation is a fresh set of proposals, not another complete opening-to-end
    # outline. Its valid events must never be replaced with old generic events.
    if continuation:
        return accepted
    tail = accepted[len(accepted) // 2:]
    mapped = {item["topic_id"] for item in tail if type(item.get("topic_id")) is int
              and 0 <= item["topic_id"] < topic_count}
    row_groups = {tuple(item.get("source_rows", [])) for item in tail if item.get("source_rows")}
    grounded_variety = (topic_count >= 6 and len(mapped) >= 6) or len(row_groups) >= 6
    if (len(accepted) < max(6, round(target / 15))
            or (target >= 80 and not grounded_variety
                and sum(item["kind"] == "interaction" for item in tail) < max(3, len(accepted) // 3))):
        return fallback_story_arc(target)
    workflow_words = re.compile(r"点赞|截图|提交|代码|联系人|经理|管理员|等待|确认")
    if (tail and not grounded_variety
            and sum(bool(workflow_words.search(item["event"])) for item in tail) * 3 >= len(tail) * 2):
        fallback = fallback_story_arc(target)
        keep = min(len(accepted), max(4, len(fallback) // 3))
        return accepted[:keep] + fallback[keep:]
    return accepted


def source_topic_arc(topics: list[dict], workflow: list[str], target: int) -> list[dict]:
    """Retain retrieved source material when a model outline cannot be used."""
    arc = [{"event": "坐席简短说明群聊用途，先来的两人从各自关注点问一件事；不一次讲完全部流程。",
            "characters": [], "kind": "interaction"}]
    for step in workflow:
        if re.search(r"等待|到账|付款|审核|\b(?:wait|await|payout)\b", step, re.I):
            continue
        arc.append({"event": f"由仍愿意尝试的人沿已公开顺序亲自完成这一步：{step}。另一人接住他的实际动作问一个新问题；不替他预设暂缓理由。",
                    "characters": [], "kind": "action"})
    for topic in topics:
        arc.append({"event": f"围绕群里这项说法展开：{topic['point']}。人物分歧：{topic['tension']}。只转述说法，不编造群外结果；让两人的具体处境与判断不同。",
                    "characters": [], "kind": "interaction", "topic_id": topic["id"],
                    "source_rows": topic["source_rows"]})
    arc.append({"event": "各人只补充有变化的选择；坐席最后一条按实际台词收束已完成与未完成的结果。",
                "characters": [], "kind": "closure"})
    return arc[:29] + [arc[-1]] if len(arc) > 30 else arc

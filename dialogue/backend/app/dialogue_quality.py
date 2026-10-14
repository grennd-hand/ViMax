from __future__ import annotations

import re

from .locale import normalize_language


# Only well-observed expressions belong here. Other locales still receive the
# cross-role review; they are not run through a translated Chinese word list.
_STATUS_NEXT = {
    "zh": re.compile(r"^(?:我这边|我也?|这边|刚才|目前)[^。！？]{0,85}(?:已|已经|刚|完成|提交|保存)[^。！？]{0,85}(?:下一步|接下来|现在准备|随后|再去|先把|先发|等|等待)"),
    "en": re.compile(r"^(?:I|I've|I have|we|we've|we have)\b.{0,150}\b(?:already|just|finished|submitted|saved|opened)\b.{0,150}\b(?:next|now|then|wait|waiting|going to|will)\b", re.I),
    "ja": re.compile(r"^(?:私|こちら|さっき)[^。！？]{0,85}(?:もう|先ほど|完了|送信|保存)[^。！？]{0,85}(?:次|これから|その後|待ち|まず)"),
}
_WAITING = {
    "zh": re.compile(r"^(?:我|我们|这边)[^。！？]{0,90}(?:已提交|已经提交|已发|已经发|提交完成)[^。！？]{0,90}(?:等|等待|待确认|没回)"),
    "en": re.compile(r"\b(?:submitted|sent it|sent the screenshot)\b.{0,100}\b(?:wait|waiting|confirmation|reply)\b", re.I),
    "ja": re.compile(r"(?:送信|提出|提出済み)[^。！？]{0,45}(?:待ち|確認|返事)"),
}
_CAVEAT = {
    "zh": re.compile(r"(?:没有|没|未)[^。！？]{0,18}(?:写|说明|明确|核实|认证|证实|保证)|(?:不能|无法)[^。！？]{0,10}(?:保证|确认)|不清楚|说不清|不填资料|不付款|不绑定"),
    "en": re.compile(r"\b(?:not verified|not confirmed|not stated|not specified|can't guarantee|cannot guarantee|won't pay|won't share personal|won't link)\b", re.I),
}
_CONFIRMATION_ECHO = {
    "zh": re.compile(r"(?:不等于|不代表|不能当|不当成|不把)[^。！？]{0,18}(?:确认|结果|到账|奖励)|(?:没有|没|未)[^。！？]{0,12}(?:回复|确认)|(?:等|看)[^。！？]{0,12}(?:明确回复|实际回复)"),
}
_REFUSAL = {
    "zh": re.compile(r"(?:^|[。！？；])\s*(?:那)?我(?:这边)?[^。！？；]{0,10}(?:不参加|不参与|不继续|不想继续|不考虑|退出|离开|先走|不做了)|(?:^|[。！？；])\s*(?:算了|先不参加|先不参与|不做了)"),
    "en": re.compile(r"(?:^|[.!?])\s*(?:I|we)\s+(?:won't|will not|don't want to|do not want to|am not going to|are not going to)\s+(?:participate|continue|join)|(?:^|[.!?])\s*(?:I'm|I am)\s+leaving", re.I),
    "ja": re.compile(r"(?:^|[。！？])\s*(?:私|僕|自分)は?[^。！？]{0,12}(?:参加しない|やめます|退出します|続けません)"),
}

_CLOSURE = {
    "zh": re.compile(r"(?:今天|大家|全群|本轮|今晚|群聊|现在的状态)(?:的讨论)?[^。！？]{0,12}(?:到此结束|对话结束|到这里|收束|收住)|(?:下次|周[一二三四五六日天])[一二三四五六七八九十\d点下午上午晚上]*见[！!。.]?\s*$"),
    "en": re.compile(r"(?:^|[.!?]\s*|[,;]\s*)(?:see you(?: all| everyone)?\b|(?:that's|that is) (?:all|it) for (?:today|now)|(?:let's|let us) (?:wrap up|call it a day)|(?:thanks|thank you),? everyone[^.!?]{0,30}\bgoodbye\b)", re.I),
    "ja": re.compile(r"(?:今日|本日|今回)[^。！？]{0,12}(?:ここまで|終了|終わり)|(?:では|それでは)?\s*(?:また来週|また明日|また次回|お疲れさまでした)[。！!]?\s*$"),
}


def closes_group(text: str, language: str | None) -> bool:
    """Detect a host's own closing move, excluding quoted examples and questions."""
    family = normalize_language(language).split("-")[0]
    pattern = _CLOSURE.get(family)
    if pattern is None:
        return False
    text = re.sub(r'「[^」]*」|“[^”]*”|"[^"\n]*"|‘[^’]*’', "", text)
    # Questions about wording or the next meeting do not close the current chat.
    sentences = re.split(r"(?<=[.!?。！？])\s*", text)
    return any(pattern.search(sentence) and not sentence.rstrip().endswith(("?", "？"))
               for sentence in sentences)


def _speaker(item: dict) -> str:
    return str(item.get("sender") or item.get("speaker") or "")


def explicit_refusal(text: str, language: str | None) -> bool | None:
    """Return None where there is no vetted local expression pack."""
    family = normalize_language(language).split("-")[0]
    pattern = _REFUSAL.get(family)
    if family == "zh":
        text = re.sub(r"(?:先|暂(?:时)?)不(?:参加|参与|继续|做了?)", "暂缓", text)
    return bool(pattern.search(text)) if pattern is not None else None


def style_findings(messages: list[dict], language: str | None) -> list[dict]:
    """Find repeated conversation moves, with evidence rather than an AI score."""
    family = normalize_language(language).split("-")[0]
    checks = (("status_next_chain", _STATUS_NEXT.get(family), "多人连续用‘已完成某步、接下来做某步’的汇报句式"),
              ("waiting_chain", _WAITING.get(family), "多人连续复述已提交后等待确认"))
    findings: list[dict] = []
    for index, current in enumerate(messages):
        if _speaker(current) == "坐席":
            continue
        for kind, pattern, reason in checks:
            if pattern is None or not pattern.search(str(current.get("content") or "")):
                continue
            window = [item for item in messages[max(0, index - 5):index + 1]
                      if _speaker(item) != "坐席" and pattern.search(str(item.get("content") or ""))]
            if len({_speaker(item) for item in window}) < 3:
                continue
            evidence = [{"seq": item.get("seq"), "speaker": _speaker(item),
                         "excerpt": str(item.get("content") or "")[:55]} for item in window[-4:]]
            findings.append({"seq": current.get("seq"), "kind": kind,
                             "reason": reason, "evidence": evidence, "count": len(window)})
            break
    caveat = _CAVEAT.get(family)
    if caveat is not None:
        for index, current in enumerate(messages):
            if not caveat.search(str(current.get("content") or "")):
                continue
            window = [item for item in messages[max(0, index - 7):index + 1]
                      if caveat.search(str(item.get("content") or ""))]
            if len(window) < 5 or len({_speaker(item) for item in window}) < 2:
                continue
            evidence = [{"seq": item.get("seq"), "speaker": _speaker(item),
                         "excerpt": str(item.get("content") or "")[:55]} for item in window[-4:]]
            findings.append({"seq": current.get("seq"), "kind": "caveat_loop",
                             "reason": "同类未知条件或风险提示反复占据对话，没有带来新事实或不同决定",
                             "evidence": evidence, "count": len(window)})
    confirmation = _CONFIRMATION_ECHO.get(family)
    if confirmation is not None:
        for index, current in enumerate(messages):
            if not confirmation.search(str(current.get("content") or "")):
                continue
            window = [item for item in messages[max(0, index - 7):index + 1]
                      if confirmation.search(str(item.get("content") or ""))]
            if len(window) < 4 or len({_speaker(item) for item in window}) < 3:
                continue
            evidence = [{"seq": item.get("seq"), "speaker": _speaker(item),
                         "excerpt": str(item.get("content") or "")[:55]} for item in window[-4:]]
            findings.append({"seq": current.get("seq"), "kind": "confirmation_loop",
                             "reason": "多人反复说明发送或等待不等于确认，话题没有发生新变化",
                             "evidence": evidence, "count": len(window)})
    return findings

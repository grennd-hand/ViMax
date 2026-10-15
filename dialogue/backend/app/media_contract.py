"""V4 screenshot placeholders: structured events, never generated image assets."""
from __future__ import annotations

import re
from typing import Any


SCREENSHOT_CODE_RE = re.compile(r"SCREENSHOT_[A-Za-z0-9_-]+\Z")
IMAGE_MODE = "素材占位"
_SCREENSHOT_RE = re.compile(r"截图|截屏")
_CLAUSE_RE = re.compile(r"[^，,。！!；;\n]+")
_NOT_COMPLETED_RE = re.compile(
    r"[？?]|还没|尚未|没有|没(?:有)?(?:发|分享|传)|未(?:发|分享|传)"
    r"|准备|打算|稍后|待会|一会|等会|接下来|将要|想(?:要)?|要(?:把|将|发|分享|传)"
    r"|如果|假如|要是|一旦"
    r"|(?:不|别|不要|不用|无需)(?:再|先)?(?:发|分享|传)"
    r"|怎么|如何|是否|能否|可否|吗|么"
)
_OTHER_SPEAKER_RE = re.compile(r"(?:他|她|他们|她们|你|您|你们|别人|对方|客户\d+)(?:的|说|已|刚|把|发)|[“”「」『』\"]")
_GROUP_SENT_RE = re.compile(
    r"(?:已(?:经)?|刚(?:刚)?|发完|发好|发过|分享过|分享完|传完)"
    r"[^，,。！？!?；;\n]{0,18}(?:发|分享|传)[^，,。！？!?；;\n]{0,10}(?:群|这里)"
    r"|(?:发|分享|传)[^，,。！？!?；;\n]{0,8}(?:群(?:里|内|组)?|这里)(?:了|啦|咯|过)"
    r"|(?:已(?:经)?|刚(?:刚)?)[^，,。！？!?；;\n]{0,10}分享(?:了|过)?"
    r"|分享(?:了|过|完了)"
)


def decode_media(kind: str, code: str, content: str) -> dict[str, Any]:
    """Decode V4 media fields; image codes and modes never enter chat content."""
    kind = str(kind or "").strip()
    code = str(code or "").strip()
    content = str(content or "").strip()
    if kind not in {"文本", "图片", "图片+文本"}:
        raise ValueError("消息类型需为文本、图片或图片+文本")
    if kind == "文本":
        if not content:
            raise ValueError("文本消息必须填写内容")
        if code not in {"", "-", "0"}:
            raise ValueError("文本消息不能附带图片码，请使用-、0或留空")
        return {"type": kind, "content": content, "image_code": None, "image_mode": None}
    if not SCREENSHOT_CODE_RE.fullmatch(code):
        raise ValueError("图片码需为SCREENSHOT_加英文字母、数字、下划线或连字符")
    if kind == "图片+文本" and not content:
        raise ValueError("图片+文本消息必须填写文字内容")
    return {"type": kind, "content": content, "image_code": code, "image_mode": IMAGE_MODE}


def _claims_own_screenshot_sent(text: str) -> bool:
    """Only flag explicit completion claims, leaving semantic review to reviewers."""
    for match in _CLAUSE_RE.finditer(text):
        clause = match.group()
        if (_SCREENSHOT_RE.search(clause) and not _NOT_COMPLETED_RE.search(clause)
                and not _OTHER_SPEAKER_RE.search(clause) and _GROUP_SENT_RE.search(clause)):
            return True
    return False


def _screenshot_instruction(text: str) -> bool:
    if not _SCREENSHOT_RE.search(text) or re.search(r"[？?]|不需要|无需|不用|不必|不要|别截图", text):
        return False
    return bool(re.search(r"请|需要|要求|记得|截图.{0,12}(?:发|分享|提交|保存)|截屏.{0,12}(?:发|分享|提交|保存)", text))


def screenshot_errors(source: dict[str, Any], run: dict[str, Any]) -> list[str]:
    """Check V4 placeholder metadata and public screenshot event causality.

    Import core at call time so core.validate_run may safely call this function.
    This checks explicit public events, not task success or the truth of an image.
    """
    if run.get("config", {}).get("dialogue_flow_version", 1) < 4:
        return []
    from .core import URL_RE, group_screenshot_required

    required = group_screenshot_required(source)
    source_urls = {url for item in source.get("messages", []) if item.get("sender") == "坐席"
                   for url in URL_RE.findall(str(item.get("content") or ""))}
    entry_announced = not source_urls
    screenshot_announced = False
    image_owners: set[str] = set()
    participant_image = False
    errors: list[str] = []
    for item in sorted(run.get("messages", []), key=lambda message: message["seq"]):
        seq, sender = item["seq"], item.get("sender", "")
        text = str(item.get("content") or "")
        valid_image = False
        try:
            media = decode_media(item.get("type"), item.get("image_code"), text)
            mode = item.get("image_mode")
            if media["type"] == "文本" and mode not in {None, ""}:
                errors.append(f"消息 {seq} 的文本消息不能设置图片方式")
            elif media["type"] != "文本":
                if mode != IMAGE_MODE:
                    errors.append(f"消息 {seq} 的图片方式必须为素材占位")
                else:
                    valid_image = True
        except ValueError as exc:
            errors.append(f"消息 {seq}：{exc}")

        if valid_image:
            if not entry_announced:
                errors.append(f"消息 {seq} 在坐席公开活动入口前发送截图事件")
            if not screenshot_announced:
                errors.append(f"消息 {seq} 在坐席公开截图要求前发送截图事件")
            if entry_announced and screenshot_announced:
                image_owners.add(sender)
                participant_image = participant_image or sender != "坐席"
        if required and sender != "坐席" and _claims_own_screenshot_sent(text) and sender not in image_owners:
            errors.append(f"消息 {seq} 声称本人的截图已发群或已分享，但没有本人先前或同条截图事件")

        # The current message cannot retroactively authorize its own image event.
        if sender == "坐席":
            if source_urls.intersection(URL_RE.findall(text)):
                entry_announced = True
            if _screenshot_instruction(text):
                screenshot_announced = True
    if required and run.get("status") == "completed" and not participant_image:
        errors.append("母本要求群内截图，完成全稿至少需要一位参与者的截图事件")
    return errors

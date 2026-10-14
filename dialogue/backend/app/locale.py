from __future__ import annotations

import re


_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_ALIASES = {"中文": "zh-CN", "简体中文": "zh-CN", "繁體中文": "zh-TW", "英语": "en-US", "英文": "en-US"}
_NAMES = {
    "zh": "中文", "en": "English", "ja": "日本語", "ko": "한국어",
    "es": "Español", "fr": "Français", "de": "Deutsch", "pt": "Português",
    "it": "Italiano", "ru": "Русский", "ar": "العربية", "hi": "हिन्दी",
    "th": "ไทย", "vi": "Tiếng Việt", "id": "Bahasa Indonesia",
}


def normalize_language(value: str | None) -> str:
    """Keep a stable BCP-47-like language tag in run data, including old Chinese runs."""
    raw = "zh-CN" if value is None else value.strip()
    raw = _ALIASES.get(raw, raw.replace("_", "-"))
    if len(raw) > 35 or not _TAG_RE.fullmatch(raw):
        raise ValueError("输出语言请填写语言地区代码，例如 zh-CN、en-US、ja-JP")
    parts = raw.split("-")
    return "-".join([parts[0].lower()] + [part.upper() if len(part) == 2 and part.isalpha()
                                           else part.title() if len(part) == 4 and part.isalpha()
                                           else part for part in parts[1:]])


def language_context(value: str | None) -> dict[str, str]:
    code = normalize_language(value)
    return {"code": code, "name": _NAMES.get(code.split("-")[0], code),
            "speech_rule": "整句台词使用此目标语言；不同语言的母本说明、历史引文与口语示例须转述为目标语言。链接、联系人、代码及人物称呼保留原值。"}


def message_char_limit(value: str | None, *, seat: bool) -> int:
    family = normalize_language(value).split("-")[0]
    if family in {"zh", "ja", "ko"}:
        return 220 if seat else 130
    return 320 if seat else 200

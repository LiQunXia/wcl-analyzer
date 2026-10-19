"""Request-local language shared by the server and export/verification helpers."""

import os
from contextvars import ContextVar


def normalize_language(value):
    value = str(value or "").strip().lower().replace("_", "-")
    return "en" if value in {"en", "en-us", "en-gb", "enus", "engb"} else "zh-CN"


LANGUAGE = ContextVar("wcl_language", default=normalize_language(os.environ.get("WCL_LANGUAGE")))


def language():
    return normalize_language(LANGUAGE.get())


def translate(zh, en):
    return en if language() == "en" else zh

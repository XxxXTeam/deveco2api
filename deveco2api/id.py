# -*- coding: utf-8 -*-
"""复刻 packages/opencode/src/id/id.ts 的 ID 生成逻辑。"""

from __future__ import annotations

import os
import threading
import time


_PREFIXES = {
    "job": "job",
    "event": "evt",
    "session": "ses",
    "message": "msg",
    "permission": "per",
    "question": "que",
    "part": "prt",
    "pty": "pty",
    "tool": "tool",
    "workspace": "wrk",
}

_LENGTH = 26
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

_lock = threading.Lock()
_last_timestamp = 0
_counter = 0


def _random_base62(length: int) -> str:
    """生成指定长度的 base62 随机字符串。"""
    return "".join(_CHARS[b % 62] for b in os.urandom(length))


def create(prefix: str, direction: str = "ascending", timestamp: float | None = None) -> str:
    """生成 ses_/msg_ 等 ID。"""
    global _last_timestamp, _counter
    current_timestamp = int(timestamp if timestamp is not None else time.time() * 1000)

    with _lock:
        if current_timestamp != _last_timestamp:
            _last_timestamp = current_timestamp
            _counter = 0
        _counter += 1
        counter = _counter

    now = (current_timestamp * 0x1000) + counter
    if direction == "descending":
        now = ~now

    now = now & ((1 << 48) - 1)  # 保证 6 字节
    time_bytes = now.to_bytes(6, "big")
    return f"{prefix}_{time_bytes.hex()}{_random_base62(_LENGTH - 12)}"


def ascending(prefix_key: str, given: str | None = None) -> str:
    prefix = _PREFIXES[prefix_key]
    if given is None:
        return create(prefix, "ascending")
    if not given.startswith(prefix):
        raise ValueError(f"ID {given} does not start with {prefix}")
    return given


def descending(prefix_key: str, given: str | None = None) -> str:
    prefix = _PREFIXES[prefix_key]
    if given is None:
        return create(prefix, "descending")
    if not given.startswith(prefix):
        raise ValueError(f"ID {given} does not start with {prefix}")
    return given


def session_id(given: str | None = None) -> str:
    return descending("session", given)


def message_id(given: str | None = None) -> str:
    return ascending("message", given)


def chat_id() -> str:
    """生成 32 位小写十六进制 UUID（去横线）。"""
    return "".join(f"{b:02x}" for b in os.urandom(16))

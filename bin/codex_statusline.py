#!/usr/bin/env python3
"""Local Codex session monitor."""

from __future__ import annotations

import argparse
import base64
import codecs
from collections.abc import Callable
from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def resolve_state_db(home: Path) -> Path:
    """Highest-versioned state_N.sqlite in CODEX_HOME. Codex bumps the numeric
    suffix on schema changes; pinning state_5 means a future state_6 either
    renders frozen data or 404s. Falls back to state_5 so a missing DB still
    names the file the error message expects."""
    best_version = -1
    best_path = home / "state_5.sqlite"
    for path in home.glob("state_*.sqlite"):
        try:
            version = int(path.stem.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if version > best_version:
            best_version, best_path = version, path
    return best_path


STATE_DB = resolve_state_db(CODEX_HOME)
CONFIG_FILE = Path(os.environ.get("CODEX_STATUSLINE_CONFIG") or CODEX_HOME / "statusline.conf")
DEFAULT_BAR_WIDTH = 15
MAX_ROLLOUT_STATE_CACHE = 256
MAX_ROLLOUT_SWEEP_CACHE = MAX_ROLLOUT_STATE_CACHE + 1
ROLLOUT_READ_BYTES = 64 * 1024
MAX_BUFFERED_ROLLOUT_LINE_BYTES = 1_048_576
ROLLOUT_PROBE_BYTES = 256
MAX_JSON_DEPTH = 256
MAX_OPEN_TURN_IDS = 256
MAX_RECENT_CLOSED_TURN_IDS = 256
MAX_ACTIVE_TOOL_CALLS = 256
MAX_RUNNING_SHELLS = 256
MAX_TOKEN_BOUNDARIES = 4
MAX_TOKEN_POINTS = 64
MAX_JSON_KEY_CHARS = 128
MAX_PROJECTED_JSON_FIELDS = 256
COMPACTED_ROLLOUT_PREFIX = re.compile(
    rb'^\{"timestamp":"([0-9T:.+\-Z]{1,64})","type":"compacted","payload":'
)
JSON_NUMBER = re.compile(
    rb'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?'
)
JSON_STRING_SPECIAL = re.compile(rb'["\\\x00-\x1f\x80-\xff]')
JSON_WHITESPACE = b" \t\r\n"
JSON_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")
TOOL_SESSION_PATTERN = re.compile(
    r"""session_id["']?\s*:\s*["']?([A-Za-z0-9_-]+)"""
)
TOOL_SESSION_VALUE_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
NON_WHITESPACE_PATTERN = re.compile(r"\S")


def terminal_size() -> os.terminal_size:
    return shutil.get_terminal_size((120, 40))


def default_width() -> int:
    try:
        return int(os.environ.get("COLUMNS") or terminal_size().columns)
    except ValueError:
        return terminal_size().columns


class Palette:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.blue = self._c("38;2;0;153;255")
        self.orange = self._c("38;2;255;176;85")
        self.green = self._c("38;2;0;175;80")
        self.cyan = self._c("38;2;86;182;194")
        self.red = self._c("38;2;255;85;85")
        self.yellow = self._c("38;2;230;200;0")
        self.white = self._c("38;2;220;220;220")
        self.magenta = self._c("38;2;180;140;255")
        self.dim = self._c("38;2;120;120;120")
        self.reset = "\033[0m" if enabled else ""

    def _c(self, code: str) -> str:
        return f"\033[{code}m" if self.enabled else ""


@dataclass
class Thread:
    id: str
    source: str
    rollout_path: str
    created_at: int
    updated_at: int
    cwd: str
    title: str
    tokens_used: int
    model: str
    reasoning_effort: str
    sandbox_policy: str
    approval_mode: str
    git_branch: str
    archived: int
    agent_path: str = ""
    agent_nickname: str = ""


@dataclass(frozen=True)
class GitInfo:
    repo: str
    display_branch: str
    root: str
    branch_name: str
    head: str


@dataclass
class TokenSummary:
    session: int
    today: int
    week: int
    lifetime: int
    threads_today: int
    threads_total: int


@dataclass
class CodexUsage:
    context_window: int
    context_used: int
    turn_total: int
    turn_cached: int
    session_total: int
    session_input: int
    session_cached: int
    session_output: int
    session_reasoning: int
    rate_limits: dict[str, Any]


@dataclass
class RolloutActivity:
    turns_started: int
    turns_completed: int
    turns_aborted: int
    compactions: int
    tool_calls: int
    shell_calls: int
    patch_calls: int
    active_tools: int
    active_shells: int
    active_turn_seconds: int
    last_event: str
    last_command: str
    last_user_message: str
    last_agent_message: str
    active_tool: str
    last_tool: str


@dataclass
class ActivityState:
    boundary_found: bool = True
    started_turns: OrderedDict[str, int] = field(default_factory=OrderedDict)
    recent_closed_turns: OrderedDict[str, None] = field(default_factory=OrderedDict)
    pending_tools: OrderedDict[str, str] = field(default_factory=OrderedDict)
    pending_shells: set[str] = field(default_factory=set)
    running_shells: OrderedDict[str, None] = field(default_factory=OrderedDict)
    tool_sessions: dict[str, str] = field(default_factory=dict)
    turns_started: int = 0
    turns_completed: int = 0
    turns_aborted: int = 0
    compactions: int = 0
    tool_calls: int = 0
    shell_calls: int = 0
    patch_calls: int = 0
    last_event: str = ""
    last_command: str = ""
    last_user_message: str = ""
    last_agent_message: str = ""
    last_tool: str = ""


@dataclass
class JsonStringDecoder:
    sink: Any
    escaped: bool = False
    unicode_digits: bytearray = field(default_factory=bytearray)
    unicode_remaining: int = 0
    pending_high_surrogate: int | None = None
    utf8_decoder: Any = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace")
    )

    def emit_text(self, text: str) -> None:
        if not text:
            return
        if self.pending_high_surrogate is not None:
            self.sink("\ufffd")
            self.pending_high_surrogate = None
        self.sink(text)

    def emit_codepoint(self, codepoint: int) -> None:
        if self.pending_high_surrogate is not None:
            if 0xDC00 <= codepoint <= 0xDFFF:
                high = self.pending_high_surrogate
                self.pending_high_surrogate = None
                combined = 0x10000 + ((high - 0xD800) << 10) + (codepoint - 0xDC00)
                self.sink(chr(combined))
                return
            self.sink("\ufffd")
            self.pending_high_surrogate = None
        if 0xD800 <= codepoint <= 0xDBFF:
            self.pending_high_surrogate = codepoint
        elif 0xDC00 <= codepoint <= 0xDFFF:
            self.sink("\ufffd")
        else:
            self.sink(chr(codepoint))

    def feed(self, data: bytes) -> None:
        offset = 0
        escapes = {
            ord('"'): '"',
            ord("\\"): "\\",
            ord("/"): "/",
            ord("b"): "\b",
            ord("f"): "\f",
            ord("n"): "\n",
            ord("r"): "\r",
            ord("t"): "\t",
        }
        while offset < len(data):
            if self.unicode_remaining:
                taken = min(self.unicode_remaining, len(data) - offset)
                self.unicode_digits.extend(data[offset : offset + taken])
                offset += taken
                self.unicode_remaining -= taken
                if not self.unicode_remaining:
                    try:
                        codepoint = int(self.unicode_digits, 16)
                    except ValueError:
                        codepoint = 0xFFFD
                    self.emit_codepoint(codepoint)
                    self.unicode_digits.clear()
                continue
            if self.escaped:
                value = data[offset]
                offset += 1
                self.escaped = False
                if value == ord("u"):
                    self.unicode_digits.clear()
                    self.unicode_remaining = 4
                    continue
                if self.pending_high_surrogate is not None:
                    self.sink("\ufffd")
                    self.pending_high_surrogate = None
                escaped = escapes.get(value)
                if escaped is not None:
                    self.sink(escaped)
                continue

            slash = data.find(b"\\", offset)
            end = len(data) if slash < 0 else slash
            decoded = self.utf8_decoder.decode(data[offset:end], final=False)
            self.emit_text(decoded)
            offset = end
            if slash < 0:
                return
            self.escaped = True
            offset += 1

    def finish(self) -> None:
        self.emit_text(self.utf8_decoder.decode(b"", final=True))
        if self.pending_high_surrogate is not None:
            self.sink("\ufffd")
            self.pending_high_surrogate = None


@dataclass
class BoundedJsonStringCapture:
    limit: int
    normalize_whitespace: bool = False
    chars: list[str] = field(default_factory=list)
    pending_space: bool = False
    overflow: bool = False
    decoder: JsonStringDecoder = field(init=False)

    def __post_init__(self) -> None:
        self.decoder = JsonStringDecoder(self.accept_text)

    def accept_text(self, text: str) -> None:
        if self.overflow:
            return
        if not self.normalize_whitespace:
            remaining = self.limit + 1 - len(self.chars)
            self.chars.extend(text[:remaining])
            self.overflow = len(text) > remaining or len(self.chars) > self.limit
            return
        for match in re.finditer(r"\s+|\S+", text):
            value = match.group(0)
            if value.isspace():
                self.pending_space = bool(self.chars)
                continue
            if self.pending_space:
                self.chars.append(" ")
                self.pending_space = False
            remaining = self.limit + 1 - len(self.chars)
            self.chars.extend(value[:remaining])
            if len(value) > remaining or len(self.chars) > self.limit:
                self.overflow = True
                return

    def feed(self, data: bytes) -> None:
        self.decoder.feed(data)

    def finish(self) -> str:
        self.decoder.finish()
        value = "".join(self.chars)
        if self.normalize_whitespace and (self.overflow or len(value) > self.limit):
            return value[: self.limit - 1] + "…"
        return value[: self.limit]


@dataclass
class JsonFrame:
    kind: str
    state: str
    path: tuple[str | int, ...]
    key: str = ""
    index: int = 0


@dataclass
class JsonValueScanner:
    observer: Any = None
    stack: list[JsonFrame] = field(default_factory=list)
    mode: str = ""
    string_is_key: bool = False
    string_path: tuple[str | int, ...] = ()
    string_capture: Any = None
    unicode_digits_remaining: int = 0
    literal: bytes = b""
    literal_index: int = 0
    scalar_path: tuple[str | int, ...] = ()
    number_state: str = ""
    number_buffer: bytearray = field(default_factory=bytearray)
    number_truncated: bool = False
    utf8_remaining: int = 0
    utf8_min: int = 0x80
    utf8_max: int = 0xBF
    complete: bool = False
    invalid: bool = False

    def next_value_path(self) -> tuple[str | int, ...]:
        if not self.stack:
            return ()
        frame = self.stack[-1]
        if frame.kind == "object":
            return (*frame.path, frame.key)
        return (*frame.path, frame.index)

    def finish_value(self) -> None:
        if not self.stack:
            self.complete = True
            return
        frame = self.stack[-1]
        if frame.kind == "object" and frame.state == "value":
            frame.state = "comma_or_end"
            frame.key = ""
            return
        if frame.kind == "array" and frame.state in {"value_or_end", "value"}:
            frame.state = "comma_or_end"
            frame.index += 1
            return
        self.invalid = True

    def finish_string(self) -> None:
        self.mode = ""
        result = self.string_capture.finish() if self.string_capture is not None else None
        capture = self.string_capture
        self.string_capture = None
        if self.string_is_key:
            if not self.stack or self.stack[-1].kind != "object":
                self.invalid = True
                return
            frame = self.stack[-1]
            frame.key = (
                result
                if isinstance(result, str)
                and not getattr(capture, "overflow", False)
                else "\0"
            )
            frame.state = "colon"
            return
        if self.observer is not None and capture is not None:
            self.observer.string_value(self.string_path, result)
        self.finish_value()

    def feed_capture(self, data: bytes) -> None:
        if self.string_capture is not None and data:
            self.string_capture.feed(data)

    def start_utf8(self, value: int) -> None:
        if 0xC2 <= value <= 0xDF:
            self.utf8_remaining = 1
            self.utf8_min, self.utf8_max = 0x80, 0xBF
        elif value == 0xE0:
            self.utf8_remaining = 2
            self.utf8_min, self.utf8_max = 0xA0, 0xBF
        elif 0xE1 <= value <= 0xEC or 0xEE <= value <= 0xEF:
            self.utf8_remaining = 2
            self.utf8_min, self.utf8_max = 0x80, 0xBF
        elif value == 0xED:
            self.utf8_remaining = 2
            self.utf8_min, self.utf8_max = 0x80, 0x9F
        elif value == 0xF0:
            self.utf8_remaining = 3
            self.utf8_min, self.utf8_max = 0x90, 0xBF
        elif 0xF1 <= value <= 0xF3:
            self.utf8_remaining = 3
            self.utf8_min, self.utf8_max = 0x80, 0xBF
        elif value == 0xF4:
            self.utf8_remaining = 3
            self.utf8_min, self.utf8_max = 0x80, 0x8F
        else:
            self.invalid = True

    def feed_string(self, data: bytes, offset: int) -> int:
        while offset < len(data) and not self.invalid and self.mode:
            value = data[offset]
            if self.utf8_remaining:
                self.feed_capture(data[offset : offset + 1])
                if not self.utf8_min <= value <= self.utf8_max:
                    self.invalid = True
                    break
                self.utf8_remaining -= 1
                self.utf8_min, self.utf8_max = 0x80, 0xBF
                offset += 1
                continue
            if self.mode == "unicode":
                self.feed_capture(data[offset : offset + 1])
                if value not in JSON_HEX_DIGITS:
                    self.invalid = True
                    break
                self.unicode_digits_remaining -= 1
                offset += 1
                if not self.unicode_digits_remaining:
                    self.mode = "string"
                continue
            if self.mode == "escape":
                self.feed_capture(data[offset : offset + 1])
                offset += 1
                if value in b'"\\/bfnrt':
                    self.mode = "string"
                elif value == ord("u"):
                    self.mode = "unicode"
                    self.unicode_digits_remaining = 4
                else:
                    self.invalid = True
                continue

            match = JSON_STRING_SPECIAL.search(data, offset)
            if match is None:
                self.feed_capture(data[offset:])
                return len(data)
            self.feed_capture(data[offset : match.start()])
            offset = match.start()
            value = data[offset]
            offset += 1
            if value == ord('"'):
                self.finish_string()
            elif value == ord("\\"):
                self.feed_capture(b"\\")
                self.mode = "escape"
            elif value < 0x20:
                self.invalid = True
            else:
                self.feed_capture(bytes((value,)))
                self.start_utf8(value)
        return offset

    def feed_number(self, value: int) -> bool:
        digit = ord("0") <= value <= ord("9")
        if self.number_state == "sign":
            if value == ord("0"):
                self.number_state = "zero"
            elif ord("1") <= value <= ord("9"):
                self.number_state = "int"
            else:
                self.invalid = True
            return True
        if self.number_state == "zero":
            if digit:
                self.invalid = True
                return True
            if value == ord("."):
                self.number_state = "dot"
                return True
            if value in b"eE":
                self.number_state = "exp"
                return True
            return False
        if self.number_state == "int":
            if digit:
                return True
            if value == ord("."):
                self.number_state = "dot"
                return True
            if value in b"eE":
                self.number_state = "exp"
                return True
            return False
        if self.number_state == "dot":
            if digit:
                self.number_state = "frac"
            else:
                self.invalid = True
            return True
        if self.number_state == "frac":
            if digit:
                return True
            if value in b"eE":
                self.number_state = "exp"
                return True
            return False
        if self.number_state == "exp":
            if value in b"+-":
                self.number_state = "exp_sign"
            elif digit:
                self.number_state = "exp_digits"
            else:
                self.invalid = True
            return True
        if self.number_state == "exp_sign":
            if digit:
                self.number_state = "exp_digits"
            else:
                self.invalid = True
            return True
        if self.number_state == "exp_digits":
            return digit
        self.invalid = True
        return True

    def number_complete(self) -> bool:
        return self.number_state in {"zero", "int", "frac", "exp_digits"}

    def finish_number(self) -> None:
        if self.observer is not None and not self.number_truncated:
            try:
                value = json.loads(self.number_buffer)
            except (ValueError, json.JSONDecodeError):
                value = None
            else:
                self.observer.scalar_value(self.scalar_path, value)
        self.number_buffer.clear()
        self.number_truncated = False
        self.mode = ""
        self.finish_value()

    def start_value(self, value: int) -> None:
        path = self.next_value_path()
        if value == ord('"'):
            self.mode = "string"
            self.string_is_key = False
            self.string_path = path
            self.string_capture = (
                self.observer.string_capture(path) if self.observer is not None else None
            )
            return
        if value in {ord("{"), ord("[")}:
            if len(self.stack) >= MAX_JSON_DEPTH:
                self.invalid = True
                return
            kind = "object" if value == ord("{") else "array"
            state = "key_or_end" if kind == "object" else "value_or_end"
            if self.observer is not None:
                self.observer.start_container(path, kind)
            self.stack.append(JsonFrame(kind, state, path))
            return
        self.scalar_path = path
        if value == ord("-"):
            self.mode = "number"
            self.number_state = "sign"
        elif value == ord("0"):
            self.mode = "number"
            self.number_state = "zero"
        elif ord("1") <= value <= ord("9"):
            self.mode = "number"
            self.number_state = "int"
        else:
            literals = {
                ord("t"): b"true",
                ord("f"): b"false",
                ord("n"): b"null",
            }
            if value not in literals:
                self.invalid = True
                return
            self.mode = "literal"
            self.literal = literals[value]
            self.literal_index = 1
            return
        self.number_buffer = bytearray((value,))
        self.number_truncated = False

    def close_container(self) -> None:
        frame = self.stack.pop()
        if self.observer is not None:
            self.observer.end_container(frame.path, frame.kind)
        self.finish_value()

    def feed(self, data: bytes) -> int:
        offset = 0
        while offset < len(data) and not self.complete and not self.invalid:
            if self.mode in {"string", "escape", "unicode"}:
                offset = self.feed_string(data, offset)
                continue
            if self.mode == "literal":
                value = data[offset]
                if (
                    self.literal_index >= len(self.literal)
                    or value != self.literal[self.literal_index]
                ):
                    self.invalid = True
                    break
                self.literal_index += 1
                offset += 1
                if self.literal_index == len(self.literal):
                    if self.observer is not None:
                        literal_value = {
                            b"true": True,
                            b"false": False,
                            b"null": None,
                        }[self.literal]
                        self.observer.scalar_value(self.scalar_path, literal_value)
                    self.mode = ""
                    self.finish_value()
                continue
            if self.mode == "number":
                value = data[offset]
                if self.feed_number(value):
                    if len(self.number_buffer) < 128:
                        self.number_buffer.append(value)
                    else:
                        self.number_truncated = True
                    offset += 1
                    continue
                if not self.number_complete():
                    self.invalid = True
                    break
                self.finish_number()
                continue

            if not self.stack:
                while offset < len(data) and data[offset] in JSON_WHITESPACE:
                    offset += 1
                if offset < len(data):
                    self.start_value(data[offset])
                    offset += 1
                continue

            frame = self.stack[-1]
            while offset < len(data) and data[offset] in JSON_WHITESPACE:
                offset += 1
            if offset >= len(data):
                break
            value = data[offset]

            if frame.kind == "object" and frame.state in {"key_or_end", "key"}:
                if frame.state == "key_or_end" and value == ord("}"):
                    self.close_container()
                    offset += 1
                elif value == ord('"'):
                    self.mode = "string"
                    self.string_is_key = True
                    self.string_capture = BoundedJsonStringCapture(MAX_JSON_KEY_CHARS)
                    offset += 1
                else:
                    self.invalid = True
                continue
            if frame.kind == "object" and frame.state == "colon":
                if value != ord(":"):
                    self.invalid = True
                else:
                    frame.state = "value"
                    offset += 1
                continue
            if frame.state == "comma_or_end":
                closing_byte = ord("}") if frame.kind == "object" else ord("]")
                if value == closing_byte:
                    self.close_container()
                    offset += 1
                elif value == ord(","):
                    frame.state = "key" if frame.kind == "object" else "value"
                    offset += 1
                else:
                    self.invalid = True
                continue
            if frame.kind == "array" and frame.state == "value_or_end" and value == ord("]"):
                self.close_container()
                offset += 1
                continue
            self.start_value(value)
            offset += 1

        if self.complete and self.observer is not None:
            self.observer.finish()
        return offset


@dataclass
class EmbeddedToolProjection:
    session_any_depth: bool = False
    root_kind: str = ""
    values: dict[tuple[str | int, ...], Any] = field(default_factory=dict)
    command_parts: dict[str, list[str]] = field(
        default_factory=lambda: {"cmd": [], "command": []}
    )
    command_seen: set[str] = field(default_factory=set)

    def start_container(self, path: tuple[str | int, ...], kind: str) -> None:
        if not path:
            self.root_kind = kind
        if len(path) == 1 and path[0] in self.command_parts:
            self.command_seen.add(str(path[0]))

    def end_container(self, path: tuple[str | int, ...], kind: str) -> None:
        return

    def finish(self) -> None:
        return

    def string_capture(
        self,
        path: tuple[str | int, ...],
    ) -> BoundedJsonStringCapture | None:
        if path in {("cmd",), ("command",), ("session_id",)} or (
            self.session_any_depth and path and path[-1] == "session_id"
        ):
            limit = 1024 if path[0] in {"cmd", "command"} else 256
            return BoundedJsonStringCapture(limit)
        if (
            len(path) == 2
            and path[0] in {"cmd", "command"}
            and isinstance(path[1], int)
        ):
            return BoundedJsonStringCapture(1024)
        return None

    def append_command_part(self, key: str, value: Any) -> None:
        self.command_seen.add(key)
        parts = self.command_parts[key]
        if (
            len(parts) >= MAX_PROJECTED_JSON_FIELDS
            or sum(len(part) for part in parts) >= 1024
        ):
            return
        parts.append(str(value))

    def string_value(self, path: tuple[str | int, ...], value: Any) -> None:
        self.scalar_value(path, value)

    def scalar_value(self, path: tuple[str | int, ...], value: Any) -> None:
        if self.session_any_depth and path and path[-1] == "session_id":
            self.values[("session_id",)] = value
            return
        if path in {("session_id",), ("exit_code",), ("cmd",), ("command",)}:
            self.values[path] = value
            if path[0] in {"cmd", "command"}:
                self.command_seen.add(str(path[0]))
            return
        if (
            len(path) == 2
            and path[0] in {"cmd", "command"}
            and isinstance(path[1], int)
        ):
            self.append_command_part(str(path[0]), value)

    def result(self) -> dict[str, Any] | None:
        if self.root_kind != "object":
            return None
        result: dict[str, Any] = {}
        for key in ("session_id", "exit_code"):
            path = (key,)
            if path in self.values:
                result[key] = self.values[path]
        command_key = "cmd" if "cmd" in self.command_seen else "command"
        command = self.values.get((command_key,))
        if isinstance(command, str):
            result["command"] = command
        elif self.command_parts[command_key]:
            result["command"] = " ".join(self.command_parts[command_key])
        return result


@dataclass
class StreamingToolSessionCapture:
    state: str = "search"
    search_tail: str = ""
    value_chars: list[str] = field(default_factory=list)
    quote: str = ""
    unicode_digits: str = ""
    session_id: str = ""

    def reset_candidate(self) -> None:
        self.state = "search"
        self.search_tail = ""
        self.value_chars.clear()
        self.quote = ""
        self.unicode_digits = ""

    def append_value(self, value: str) -> None:
        remaining = 256 - len(self.value_chars)
        if remaining > 0:
            self.value_chars.extend(value[:remaining])

    def finish_candidate(self) -> None:
        if self.value_chars:
            self.session_id = "".join(self.value_chars)
            self.state = "done"
        else:
            self.reset_candidate()

    def feed(self, text: str) -> None:
        offset = 0
        key = "session_id"
        while offset < len(text) and not self.session_id:
            if self.state == "search":
                if self.search_tail:
                    tail_size = len(self.search_tail)
                    candidate = self.search_tail + text[offset:]
                    found = candidate.find(key)
                    if found < 0:
                        self.search_tail = candidate[-(len(key) - 1) :]
                        return
                    offset += found + len(key) - tail_size
                    self.search_tail = ""
                else:
                    found = text.find(key, offset)
                    if found < 0:
                        self.search_tail = text[offset:][-(len(key) - 1) :]
                        return
                    offset = found + len(key)
                self.state = "after_key"
                continue

            if self.state == "after_key":
                if text[offset] in "\"'":
                    offset += 1
                self.state = "before_colon"
                continue

            if self.state in {"before_colon", "before_value"}:
                match = NON_WHITESPACE_PATTERN.search(text, offset)
                if match is None:
                    return
                offset = match.start()
                if self.state == "before_colon":
                    if text[offset] != ":":
                        self.reset_candidate()
                        continue
                    offset += 1
                    self.state = "before_value"
                    continue
                if text[offset] in "\"'":
                    self.quote = text[offset]
                    offset += 1
                self.state = "value"
                continue

            if self.state == "value":
                match = TOOL_SESSION_VALUE_PATTERN.match(text, offset)
                if match is not None:
                    self.append_value(match.group(0))
                    offset = match.end()
                    if offset == len(text):
                        return
                value = text[offset]
                if self.quote and value == "\\":
                    offset += 1
                    self.state = "escape"
                    continue
                if self.quote and value == self.quote:
                    offset += 1
                    self.finish_candidate()
                    continue
                self.finish_candidate()
                if not self.session_id:
                    offset += 1
                continue

            if self.state == "escape":
                value = text[offset]
                offset += 1
                if value == "u":
                    self.unicode_digits = ""
                    self.state = "unicode"
                    continue
                decoded = {
                    '"': '"',
                    "\\": "\\",
                    "/": "/",
                    "b": "\b",
                    "f": "\f",
                    "n": "\n",
                    "r": "\r",
                    "t": "\t",
                }.get(value)
                if decoded is not None and TOOL_SESSION_VALUE_PATTERN.fullmatch(decoded):
                    self.append_value(decoded)
                    self.state = "value"
                    continue
                self.finish_candidate()
                continue

            needed = 4 - len(self.unicode_digits)
            taken = min(needed, len(text) - offset)
            self.unicode_digits += text[offset : offset + taken]
            offset += taken
            if len(self.unicode_digits) < 4:
                return
            try:
                decoded = chr(int(self.unicode_digits, 16))
            except ValueError:
                decoded = ""
            if TOOL_SESSION_VALUE_PATTERN.fullmatch(decoded):
                self.append_value(decoded)
                self.state = "value"
                continue
            self.finish_candidate()

    def finish(self) -> str:
        if (
            not self.session_id
            and self.value_chars
            and self.state in {"value", "escape", "unicode"}
        ):
            self.finish_candidate()
        return self.session_id


@dataclass
class EmbeddedJsonStringCapture:
    session_any_depth: bool = False
    projection: EmbeddedToolProjection = field(init=False)
    scanner: JsonValueScanner = field(init=False)
    decoder: JsonStringDecoder = field(init=False)
    raw_fallback: BoundedJsonStringCapture = field(init=False)
    raw_session: StreamingToolSessionCapture = field(init=False)
    trailing_invalid: bool = False

    def __post_init__(self) -> None:
        self.projection = EmbeddedToolProjection(self.session_any_depth)
        self.scanner = JsonValueScanner(observer=self.projection)
        self.decoder = JsonStringDecoder(self.accept_text)
        self.raw_fallback = BoundedJsonStringCapture(
            512,
            normalize_whitespace=True,
        )
        self.raw_session = StreamingToolSessionCapture()

    def accept_text(self, text: str) -> None:
        if self.session_any_depth:
            self.raw_fallback.accept_text(text)
            self.raw_session.feed(text)
        data = text.encode("utf-8")
        if self.scanner.complete:
            if any(value not in JSON_WHITESPACE for value in data):
                self.trailing_invalid = True
            return
        consumed = self.scanner.feed(data)
        if consumed < len(data) and any(
            value not in JSON_WHITESPACE for value in data[consumed:]
        ):
            self.trailing_invalid = True

    def feed(self, data: bytes) -> None:
        self.decoder.feed(data)

    def finish(self) -> dict[str, Any] | None:
        self.decoder.finish()
        valid = not (
            self.trailing_invalid
            or self.scanner.invalid
            or not self.scanner.complete
        )
        result = self.projection.result() if valid else None
        if not self.session_any_depth:
            return result
        raw_session_id = self.raw_session.finish()
        if result is None:
            result = {}
            fallback = self.raw_fallback.finish()
            if fallback:
                result["command"] = fallback
        if raw_session_id and "session_id" not in result:
            result["session_id"] = raw_session_id
        return result or None


@dataclass
class RolloutEventProjection:
    root_kind: str = ""
    values: dict[tuple[str | int, ...], Any] = field(default_factory=dict)
    embedded_seen: set[str] = field(default_factory=set)
    embedded_inputs: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    output_result: dict[str, Any] | None = None
    command_parts: list[Any] = field(default_factory=list)
    command_seen: bool = False
    command_is_array: bool = False

    def start_container(self, path: tuple[str | int, ...], kind: str) -> None:
        if not path:
            self.root_kind = kind
        if path == ("payload", "command"):
            self.command_seen = True
            self.command_is_array = kind == "array"

    def end_container(self, path: tuple[str | int, ...], kind: str) -> None:
        return

    def finish(self) -> None:
        return

    def is_embedded_path(self, path: tuple[str | int, ...]) -> bool:
        if path in {
            ("payload", "input"),
            ("payload", "arguments"),
            ("payload", "output"),
        }:
            return True
        return (
            len(path) == 4
            and path[:2] == ("payload", "output")
            and isinstance(path[2], int)
            and path[3] == "text"
        )

    def string_capture(self, path: tuple[str | int, ...]) -> Any:
        if self.is_embedded_path(path):
            return EmbeddedJsonStringCapture(
                session_any_depth=path
                in {("payload", "input"), ("payload", "arguments")}
            )
        plain_limits = {
            ("type",): 128,
            ("timestamp",): 128,
            ("payload", "type"): 128,
            ("payload", "call_id"): 512,
            ("payload", "name"): 128,
            ("payload", "turn_id"): 512,
            ("payload", "started_at"): 64,
            ("payload", "output", "session_id"): 256,
        }
        if path in plain_limits:
            return BoundedJsonStringCapture(plain_limits[path])
        if path == ("payload", "message"):
            return BoundedJsonStringCapture(256, normalize_whitespace=True)
        if path == ("payload", "command") or (
            len(path) == 3
            and path[:2] == ("payload", "command")
            and isinstance(path[2], int)
        ):
            return BoundedJsonStringCapture(512)
        if (
            len(path) == 4
            and path[:3]
            in {
                ("payload", "rate_limits", "primary"),
                ("payload", "rate_limits", "secondary"),
            }
        ):
            return BoundedJsonStringCapture(512)
        if path == ("payload", "rate_limits", "plan_type"):
            return BoundedJsonStringCapture(64, normalize_whitespace=True)
        return None

    def set_value(self, path: tuple[str | int, ...], value: Any) -> None:
        if path in self.values or len(self.values) < MAX_PROJECTED_JSON_FIELDS:
            self.values[path] = value

    def append_command_part(self, value: Any) -> None:
        self.command_seen = True
        if (
            len(self.command_parts) < MAX_PROJECTED_JSON_FIELDS
            and sum(len(str(part)) for part in self.command_parts) < 1024
        ):
            self.command_parts.append(value)

    def string_value(self, path: tuple[str | int, ...], value: Any) -> None:
        if self.is_embedded_path(path):
            if path in {("payload", "input"), ("payload", "arguments")}:
                key = str(path[-1])
                self.embedded_seen.add(key)
                self.embedded_inputs[key] = value
            elif isinstance(value, dict):
                self.output_result = value
            return
        self.scalar_value(path, value)

    def scalar_value(self, path: tuple[str | int, ...], value: Any) -> None:
        direct_paths = {
            ("type",),
            ("timestamp",),
            ("payload", "type"),
            ("payload", "call_id"),
            ("payload", "name"),
            ("payload", "turn_id"),
            ("payload", "started_at"),
            ("payload", "message"),
            ("payload", "output", "session_id"),
            ("payload", "output", "exit_code"),
            ("payload", "info", "model_context_window"),
            ("payload", "rate_limits", "plan_type"),
        }
        if path in direct_paths:
            self.set_value(path, value)
            return
        if path == ("payload", "command"):
            self.command_seen = True
            self.set_value(path, value)
            return
        if (
            len(path) == 3
            and path[:2] == ("payload", "command")
            and isinstance(path[2], int)
        ):
            self.append_command_part(value)
            return
        if (
            len(path) == 4
            and path[:3]
            in {
                ("payload", "info", "last_token_usage"),
                ("payload", "info", "total_token_usage"),
            }
            and isinstance(value, (int, float))
        ):
            self.set_value(path, value)
            return
        if (
            len(path) == 4
            and path[:3]
            in {
                ("payload", "rate_limits", "primary"),
                ("payload", "rate_limits", "secondary"),
            }
            and (isinstance(value, (int, float, str, bool)) or value is None)
        ):
            self.set_value(path, value)

    def nested_values(self, prefix: tuple[str, ...]) -> dict[str, Any]:
        return {
            str(path[-1]): value
            for path, value in self.values.items()
            if len(path) == len(prefix) + 1 and path[:-1] == prefix
        }

    def compact_event(self) -> dict[str, Any] | None:
        if self.root_kind != "object":
            return None
        item: dict[str, Any] = {"type": self.values.get(("type",), "")}
        timestamp = self.values.get(("timestamp",))
        if isinstance(timestamp, str):
            item["timestamp"] = timestamp
        payload: dict[str, Any] = {
            "type": self.values.get(("payload", "type"), "")
        }
        for key in ("call_id", "name", "turn_id", "started_at", "message"):
            path = ("payload", key)
            if path in self.values:
                payload[key] = self.values[path]

        command = self.values.get(("payload", "command"))
        if isinstance(command, str):
            payload["command"] = command
        elif self.command_is_array:
            payload["command"] = self.command_parts.copy()

        input_key = "input" if "input" in self.embedded_seen else "arguments"
        input_result = self.embedded_inputs.get(input_key)
        if isinstance(input_result, dict):
            nested_command = input_result.get("command")
            if "command" not in payload and isinstance(nested_command, str):
                payload["command"] = nested_command
            session_id = input_result.get("session_id")
            if session_id is not None:
                payload["input"] = json.dumps({"session_id": str(session_id)})

        output = self.output_result.copy() if self.output_result is not None else {}
        for key in ("session_id", "exit_code"):
            path = ("payload", "output", key)
            if path in self.values:
                output[key] = self.values[path]
        if output:
            payload["output"] = output

        info: dict[str, Any] = {}
        model_window = self.values.get(("payload", "info", "model_context_window"))
        if model_window is not None:
            info["model_context_window"] = model_window
        for key in ("last_token_usage", "total_token_usage"):
            info[key] = self.nested_values(("payload", "info", key))
        if info:
            payload["info"] = info

        rate_limits: dict[str, Any] = {}
        for key in ("primary", "secondary"):
            rate_limits[key] = self.nested_values(("payload", "rate_limits", key))
        plan_type = self.values.get(("payload", "rate_limits", "plan_type"))
        if plan_type is not None:
            rate_limits["plan_type"] = plan_type
        if rate_limits:
            payload["rate_limits"] = rate_limits

        item["payload"] = payload
        return compact_rollout_event(item)


@dataclass
class SelectiveRolloutRecordScanner:
    projection: RolloutEventProjection = field(default_factory=RolloutEventProjection)
    value: JsonValueScanner = field(init=False)
    invalid: bool = False

    def __post_init__(self) -> None:
        self.value = JsonValueScanner(observer=self.projection)

    def feed(self, data: bytes) -> None:
        if self.invalid:
            return
        if self.value.complete:
            if any(value not in JSON_WHITESPACE for value in data):
                self.invalid = True
            return
        consumed = self.value.feed(data)
        if self.value.invalid:
            self.invalid = True
            return
        if consumed < len(data) and any(
            value not in JSON_WHITESPACE for value in data[consumed:]
        ):
            self.invalid = True

    def complete(self) -> bool:
        return self.value.complete and not self.invalid

    def item(self) -> dict[str, Any] | None:
        return self.projection.compact_event() if self.complete() else None


@dataclass
class CompactedRecordScanner:
    timestamp: str
    value: JsonValueScanner = field(default_factory=JsonValueScanner)
    phase: str = "payload"
    invalid: bool = False

    def feed(self, data: bytes) -> None:
        offset = 0
        while offset < len(data) and not self.invalid:
            if self.phase == "payload":
                consumed = self.value.feed(data[offset:])
                offset += consumed
                if self.value.invalid:
                    self.invalid = True
                    return
                if not self.value.complete:
                    return
                self.phase = "outer"
                continue
            if self.phase == "outer":
                while offset < len(data) and data[offset] in JSON_WHITESPACE:
                    offset += 1
                if offset >= len(data):
                    return
                if data[offset] != ord("}"):
                    self.invalid = True
                    return
                self.phase = "trailing"
                offset += 1
                continue
            if any(value not in JSON_WHITESPACE for value in data[offset:]):
                self.invalid = True
            return

    def complete(self) -> bool:
        return self.phase == "trailing" and not self.invalid


@dataclass
class PendingRolloutLine:
    size: int = 0
    buffer: bytearray = field(default_factory=bytearray)
    compacted: CompactedRecordScanner | None = None
    selective: SelectiveRolloutRecordScanner | None = None
    trailing_only: bool = False
    discarded: bool = False
    applied: bool = False
    applied_size: int = 0


@dataclass
class RolloutStateEntry:
    inode: int
    offset: int = 0
    mtime_ns: int = 0
    head_probe: bytes = b""
    probe_offset: int = 0
    probe: bytes = b""
    pending: PendingRolloutLine = field(default_factory=PendingRolloutLine)
    activity_key: tuple[str, bool, int] = ("", False, 0)
    activity: ActivityState = field(default_factory=ActivityState)
    latest_token: dict[str, Any] = field(default_factory=dict)
    token_points: list[tuple[int, int]] = field(default_factory=list)
    token_history_dropped: bool = False
    token_count_seen: bool = False
    token_baselines: OrderedDict[int, int] = field(default_factory=OrderedDict)


ROLLOUT_STATE_CACHE: OrderedDict[str, RolloutStateEntry] = OrderedDict()


@dataclass
class RolloutStateSweep:
    encountered: OrderedDict[str, None] = field(default_factory=OrderedDict)
    spill: OrderedDict[str, RolloutStateEntry] = field(default_factory=OrderedDict)


ACTIVE_ROLLOUT_STATE_SWEEP: RolloutStateSweep | None = None


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def parse_shell_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value
    return values


def setting(name: str, config: dict[str, str], default: str = "") -> str:
    return os.environ.get(f"CODEX_{name}", os.environ.get(name, config.get(name, default)))


def int_setting(name: str, config: dict[str, str], default: int) -> int:
    try:
        return int(setting(name, config, str(default)))
    except ValueError:
        return default


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except (ValueError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=1)
def account_label() -> str:
    auth = read_json(CODEX_HOME / "auth.json")
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    claims = decode_jwt_payload(str(tokens.get("id_token", "")))
    for key in ("email", "preferred_username", "name"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    account_id = tokens.get("account_id")
    if isinstance(account_id, str) and account_id:
        return account_id[:12]
    mode = auth.get("auth_mode")
    return str(mode) if mode else "unknown"


@lru_cache(maxsize=1)
def read_codex_config() -> dict[str, Any]:
    path = CODEX_HOME / "config.toml"
    if tomllib is None:
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def sqlite_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
    conn.row_factory = sqlite3.Row
    return conn


IDLE_AFTER_SECONDS = 600
IDLE_POLL_SECONDS = 30.0
WAL_GUARD_BYTES = 128 * 1024 * 1024
WAL_GUARD_MIN_INTERVAL_SECONDS = 60.0


def next_sleep_seconds(interval: float, latest_activity_ms: int, now_ms: int) -> float:
    # Idle sessions back off so forgotten watchers can't starve WAL checkpoints.
    if latest_activity_ms and now_ms - latest_activity_ms > IDLE_AFTER_SECONDS * 1000:
        return max(IDLE_POLL_SECONDS, interval)
    return max(0.5, interval)


def snapshot_activity_ms(data: dict[str, Any], multi: bool) -> int:
    """Newest thread activity as epoch MILLISECONDS. threads.updated_at (and the
    per-session updated_at in a --top/--all snapshot) is epoch SECONDS; the ×1000
    is the fix for the watch loop treating every session as idle and sleeping the
    30s floor regardless of the requested interval."""
    if multi:
        return max(
            (int(s.get("updated_at") or 0) * 1000 for s in data.get("sessions") or []),
            default=0,
        )
    return int(data.get("updated_at") or 0) * 1000


def floor_multi_session_sleep(sleep_s: float, multi: bool) -> float:
    return max(sleep_s, IDLE_POLL_SECONDS) if multi else sleep_s


def owner_alive(owner_pid_file: str) -> bool:
    """False only for a readable pid whose process is gone; missing/partial files
    stay True because the launcher writes the pid after the footer pane starts."""
    try:
        pid = int(Path(owner_pid_file).read_text().strip())
    except (OSError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def maybe_checkpoint_wal(
    db_path: Path,
    *,
    last_attempt: float,
    now: float,
    threshold_bytes: int = WAL_GUARD_BYTES,
) -> float:
    """Best-effort TRUNCATE checkpoint once the WAL passes the threshold.

    Codex's own passive checkpoints starve under overlapping readers; left alone
    the WAL grows without bound and every query slows (observed 727 MB)."""
    wal = Path(f"{db_path}-wal")
    try:
        size = wal.stat().st_size
    except OSError:
        return last_attempt
    if size < threshold_bytes or now - last_attempt < WAL_GUARD_MIN_INTERVAL_SECONDS:
        return last_attempt
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=0.2)
        try:
            conn.execute("PRAGMA busy_timeout=200")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return now


THREAD_COLUMNS = (
    "id, source, rollout_path, created_at, updated_at, cwd, title, tokens_used, model, "
    "reasoning_effort, sandbox_policy, approval_mode, git_branch, archived, "
    "agent_path, agent_nickname"
)


def row_to_thread(row: sqlite3.Row) -> Thread:
    return Thread(
        id=row["id"],
        source=row["source"] or "",
        rollout_path=row["rollout_path"],
        created_at=int(row["created_at"] or 0),
        updated_at=int(row["updated_at"] or 0),
        cwd=row["cwd"] or "",
        title=row["title"] or "",
        tokens_used=int(row["tokens_used"] or 0),
        model=row["model"] or "",
        reasoning_effort=row["reasoning_effort"] or "",
        sandbox_policy=row["sandbox_policy"] or "",
        approval_mode=row["approval_mode"] or "",
        git_branch=row["git_branch"] or "",
        archived=int(row["archived"] or 0),
        agent_path=row["agent_path"] or "",
        agent_nickname=row["agent_nickname"] or "",
    )


def paths_related(a: str, b: str) -> bool:
    if not a or not b:
        return False
    left = os.path.abspath(os.path.expanduser(a))
    right = os.path.abspath(os.path.expanduser(b))
    return left == right or left.startswith(right + os.sep) or right.startswith(left + os.sep)


def select_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    cwd: str,
    *,
    created_after_ms: int = 0,
    updated_after_ms: int = 0,
) -> Thread | None:
    if thread_id:
        row = conn.execute(f"select {THREAD_COLUMNS} from threads where id = ?", (thread_id,)).fetchone()
        return row_to_thread(row) if row else None

    if created_after_ms:
        rows = conn.execute(
            f"select {THREAD_COLUMNS} from threads where archived = 0 "
            "and coalesce(created_at_ms, created_at * 1000) >= ? "
            "order by coalesce(created_at_ms, created_at * 1000) limit 50",
            (created_after_ms,),
        ).fetchall()
    elif updated_after_ms:
        rows = conn.execute(
            f"select {THREAD_COLUMNS} from threads where archived = 0 "
            "and coalesce(updated_at_ms, updated_at * 1000) >= ? "
            "order by coalesce(updated_at_ms, updated_at * 1000) limit 50",
            (updated_after_ms,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"select {THREAD_COLUMNS} from threads where archived = 0 "
            "order by coalesce(updated_at_ms, updated_at * 1000) desc limit 50"
        ).fetchall()
    if created_after_ms or updated_after_ms:
        target_cwd = os.path.abspath(os.path.expanduser(cwd))
        related = [
            row_to_thread(row)
            for row in rows
            if os.path.abspath(os.path.expanduser(str(row["cwd"]))) == target_cwd
        ]
    else:
        related = [row_to_thread(row) for row in rows if paths_related(cwd, str(row["cwd"]))]
    for candidate in related:
        if not is_subagent_thread(candidate):
            return candidate
    if created_after_ms or updated_after_ms:
        return None
    if related:
        return related[0]
    return row_to_thread(rows[0]) if rows else None


def process_descendants(owner_pid: int) -> set[int]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {owner_pid}

    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, parent_pid = (int(field) for field in fields)
        except ValueError:
            continue
        children.setdefault(parent_pid, []).append(pid)

    descendants = {owner_pid}
    pending = [owner_pid]
    while pending:
        parent_pid = pending.pop()
        for child_pid in children.get(parent_pid, []):
            if child_pid not in descendants:
                descendants.add(child_pid)
                pending.append(child_pid)
    return descendants


def process_rollout_paths(owner_pid: int) -> set[str]:
    pids = process_descendants(owner_pid)
    proc_root = Path("/proc")
    if proc_root.is_dir():
        paths: set[str] = set()
        for pid in pids:
            try:
                descriptors = (proc_root / str(pid) / "fd").iterdir()
                for descriptor in descriptors:
                    try:
                        target = str(descriptor.resolve(strict=True))
                    except OSError:
                        continue
                    name = Path(target).name
                    if name.startswith("rollout-") and name.endswith(".jsonl"):
                        paths.add(target)
            except OSError:
                continue
        return paths

    try:
        result = subprocess.run(
            ["lsof", "-n", "-P", "-Fn", "-p", ",".join(str(pid) for pid in sorted(pids))],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    paths = set()
    for line in result.stdout.splitlines():
        if not line.startswith("n"):
            continue
        path = line[1:]
        name = Path(path).name
        if name.startswith("rollout-") and name.endswith(".jsonl"):
            paths.add(path)
    return paths


def select_owner_thread_id(conn: sqlite3.Connection, owner_pid_file: str) -> str:
    try:
        owner_pid = int(Path(owner_pid_file).read_text().strip())
    except (OSError, ValueError):
        return ""

    rollout_paths = process_rollout_paths(owner_pid)
    if not rollout_paths:
        return ""
    placeholders = ",".join("?" for _ in rollout_paths)
    rows = conn.execute(
        f"select {THREAD_COLUMNS} from threads where rollout_path in ({placeholders}) "
        "order by coalesce(updated_at_ms, updated_at * 1000) desc",
        tuple(rollout_paths),
    ).fetchall()
    if not rows:
        # Symlinked CODEX_HOME: fd scans report realpaths while the DB stores the opened path.
        recent = conn.execute(
            f"select {THREAD_COLUMNS} from threads "
            "order by coalesce(updated_at_ms, updated_at * 1000) desc limit 200"
        ).fetchall()
        rows = [row for row in recent if os.path.realpath(row["rollout_path"]) in rollout_paths]
    # Prefer the interactive root: a nested `codex exec` in the same tree is also a root.
    fallback = ""
    for row in rows:
        thread = row_to_thread(row)
        if is_subagent_thread(thread):
            continue
        if thread.source == "cli":
            return thread.id
        if not fallback:
            fallback = thread.id
    return fallback


def select_threads(conn: sqlite3.Connection, limit: int, include_archived: bool) -> list[Thread]:
    where = "" if include_archived else "where archived = 0"
    query_limit = max(50, max(1, limit) * 4)
    rows = conn.execute(
        f"select {THREAD_COLUMNS} from threads {where} "
        "order by coalesce(updated_at_ms, updated_at * 1000) desc limit ?",
        (query_limit,),
    ).fetchall()
    threads = [row_to_thread(row) for row in rows]
    return [thread for thread in threads if Path(thread.rollout_path).is_file()][:max(1, limit)]


def select_top_threads(conn: sqlite3.Connection, limit: int) -> list[Thread]:
    try:
        rows = conn.execute(
            f"""
            with recursive recent_roots as (
                select id from threads
                where source in ('cli', 'exec') and archived = 0
                order by coalesce(updated_at_ms, updated_at * 1000) desc
                limit ?
            ), selected(id) as (
                select id from recent_roots
                union
                select edge.child_thread_id from thread_spawn_edges edge
                join selected parent on edge.parent_thread_id = parent.id
                where edge.status = 'open'
            )
            select {THREAD_COLUMNS} from threads
            where archived = 0 and id in (select id from selected)
            order by coalesce(updated_at_ms, updated_at * 1000) desc
            """,
            (max(1, limit),),
        ).fetchall()
    except sqlite3.Error:
        return select_threads(conn, limit, False)
    threads = [row_to_thread(row) for row in rows]
    return [thread for thread in threads if Path(thread.rollout_path).is_file()][:max(1, limit)]


def select_descendant_threads(conn: sqlite3.Connection, parent_thread_id: str) -> list[Thread]:
    try:
        rows = conn.execute(
            f"""
            with recursive descendants(id) as (
                select child_thread_id from thread_spawn_edges where parent_thread_id = ?
                union
                select edge.child_thread_id from thread_spawn_edges edge
                join descendants parent on edge.parent_thread_id = parent.id
            )
            select {THREAD_COLUMNS} from threads where id in (select id from descendants) and id != ?
            """,
            (parent_thread_id, parent_thread_id),
        ).fetchall()
    except sqlite3.Error:
        return []
    threads = [row_to_thread(row) for row in rows]
    return [thread for thread in threads if Path(thread.rollout_path).is_file()]


def local_midnight(now: datetime) -> int:
    local_now = datetime.fromtimestamp(now.timestamp())
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def week_start(now: datetime) -> int:
    local_now = datetime.fromtimestamp(now.timestamp())
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=start.weekday())
    return int(start.timestamp())


def token_values_since_boundaries(
    rollout_path: str,
    current_total: int,
    boundaries: tuple[int, ...],
) -> dict[int, int]:
    requested_boundaries = tuple(dict.fromkeys(boundaries))
    if rollout_path in ROLLOUT_STATE_CACHE:
        state = read_rollout_state(rollout_path)
    else:
        state = read_rollout_state(
            rollout_path,
            token_boundaries=requested_boundaries[-MAX_TOKEN_BOUNDARIES:],
        )
    unresolved: list[int] = []
    for boundary in requested_boundaries:
        if boundary in state.token_baselines:
            state.token_baselines.move_to_end(boundary)
            continue
        baseline = 0
        found = False
        for timestamp, value in state.token_points:
            if timestamp < boundary:
                baseline = value
                found = True
        if found or not state.token_history_dropped:
            state.token_baselines[boundary] = baseline
        else:
            unresolved.append(boundary)

    if unresolved:
        requested = list(state.token_baselines)
        for boundary in unresolved:
            if boundary in requested:
                requested.remove(boundary)
            requested.append(boundary)
        state = read_rollout_state(rollout_path, token_boundaries=tuple(requested))

    for boundary in requested_boundaries:
        state.token_baselines.move_to_end(boundary)
    while len(state.token_baselines) > MAX_TOKEN_BOUNDARIES:
        state.token_baselines.popitem(last=False)
    prune_token_points(state)
    if not state.token_count_seen:
        return {boundary: 0 for boundary in boundaries}
    return {
        boundary: max(0, current_total - state.token_baselines[boundary])
        for boundary in boundaries
    }


def tokens_since_boundary(rollout_path: str, current_total: int, boundary: int) -> int:
    return token_values_since_boundaries(rollout_path, current_total, (boundary,))[boundary]


def token_summary(conn: sqlite3.Connection, thread: Thread, now: datetime) -> TokenSummary:
    today_start = local_midnight(now)
    week_start_ts = week_start(now)
    row = conn.execute(
        """
        select
            coalesce(sum(case when created_at >= ? then tokens_used else 0 end), 0) as today,
            coalesce(sum(case when created_at >= ? then tokens_used else 0 end), 0) as week,
            coalesce(sum(tokens_used), 0) as lifetime,
            coalesce(sum(case when created_at >= ? then 1 else 0 end), 0) as threads_today,
            count(*) as threads_total
        from threads
        """,
        (today_start, week_start_ts, today_start),
    ).fetchone()
    today = int(row["today"] or 0)
    week = int(row["week"] or 0)
    older_active_threads = conn.execute(
        "select rollout_path, created_at, updated_at, tokens_used from threads "
        "where created_at < ? and updated_at >= ?",
        (today_start, week_start_ts),
    ).fetchall()
    for older_thread in older_active_threads:
        created_at = int(older_thread["created_at"] or 0)
        updated_at = int(older_thread["updated_at"] or 0)
        current_total = int(older_thread["tokens_used"] or 0)
        rollout_path = str(older_thread["rollout_path"] or "")
        boundaries = []
        if created_at < week_start_ts:
            boundaries.append(week_start_ts)
        if created_at < today_start and updated_at >= today_start:
            boundaries.append(today_start)
        values = token_values_since_boundaries(
            rollout_path,
            current_total,
            tuple(boundaries),
        )
        week += values.get(week_start_ts, 0)
        today += values.get(today_start, 0)
    return TokenSummary(
        session=thread.tokens_used,
        today=today,
        week=week,
        lifetime=int(row["lifetime"] or 0),
        threads_today=int(row["threads_today"] or 0),
        threads_total=int(row["threads_total"] or 0),
    )


def latest_token_count(rollout_path: str) -> dict[str, Any]:
    return read_rollout_state(rollout_path).latest_token


def compact_rollout_event(item: dict[str, Any]) -> dict[str, Any] | None:
    item_type = str(item.get("type") or "")
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "")
    compact: dict[str, Any] = {"type": item_type}
    if isinstance(item.get("timestamp"), str):
        compact["timestamp"] = item["timestamp"]

    if item_type == "compacted":
        return compact
    if item_type == "event_msg" and payload_type in {"exec_command_end", "patch_apply_end"}:
        compact["payload"] = {
            key: payload[key]
            for key in ("type", "call_id", "command")
            if key in payload
        }
        return compact
    if item_type == "event_msg":
        if payload_type not in {
            "agent_message",
            "context_compacted",
            "task_complete",
            "task_started",
            "token_count",
            "turn_aborted",
            "user_message",
        }:
            return None
        if payload_type == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            compact_info: dict[str, Any] = {}
            if isinstance(info.get("model_context_window"), int):
                compact_info["model_context_window"] = info["model_context_window"]
            for usage_key in ("last_token_usage", "total_token_usage"):
                usage = info.get(usage_key) if isinstance(info.get(usage_key), dict) else {}
                compact_info[usage_key] = {
                    key: value
                    for key, value in usage.items()
                    if isinstance(value, (int, float))
                }
            rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
            compact_limits: dict[str, Any] = {}
            for limit_key in ("primary", "secondary"):
                limit = rate_limits.get(limit_key) if isinstance(rate_limits.get(limit_key), dict) else {}
                compact_limits[limit_key] = {
                    key: value
                    for key, value in limit.items()
                    if isinstance(value, (int, float, str, bool)) or value is None
                }
            plan_type = rate_limits.get("plan_type")
            if isinstance(plan_type, str):
                compact_limits["plan_type"] = short_text(plan_type, 64)
            compact["payload"] = {
                "type": payload_type,
                "info": compact_info,
                "rate_limits": compact_limits,
            }
            return compact
        compact_payload: dict[str, Any] = {"type": payload_type}
        for key in ("turn_id", "started_at"):
            if key in payload:
                compact_payload[key] = payload[key]
        message = payload.get("message")
        if isinstance(message, str):
            compact_payload["message"] = short_text(message, 256)
        compact["payload"] = compact_payload
        return compact
    if payload_type in {"function_call", "custom_tool_call"}:
        compact_payload = {
            key: payload[key]
            for key in ("type", "call_id", "name")
            if key in payload
        }
        command = command_text(payload)
        if command:
            compact_payload["command"] = short_text(command, 512)
        session_id = tool_input_session(payload)
        if session_id:
            compact_payload["input"] = json.dumps({"session_id": session_id})
        compact["payload"] = compact_payload
        return compact
    if payload_type in {"function_call_output", "custom_tool_call_output"}:
        compact_payload = {
            key: payload[key]
            for key in ("type", "call_id")
            if key in payload
        }
        result = tool_result(payload)
        output = {key: result[key] for key in ("session_id", "exit_code") if key in result}
        if output:
            compact_payload["output"] = output
        compact["payload"] = compact_payload
        return compact
    return None


def skip_json_whitespace(data: bytes, offset: int) -> int:
    while offset < len(data) and data[offset] in JSON_WHITESPACE:
        offset += 1
    return offset


def json_string_end(data: bytes, offset: int) -> int | None:
    if offset >= len(data) or data[offset] != ord('"'):
        return None
    offset += 1
    while match := JSON_STRING_SPECIAL.search(data, offset):
        special = data[match.start()]
        if special == ord('"'):
            return match.end()
        if special != ord("\\"):
            return None
        offset = match.end()
        if offset >= len(data):
            return None
        escaped = data[offset]
        if escaped in b'"\\/bfnrt':
            offset += 1
            continue
        if escaped != ord("u") or offset + 4 >= len(data):
            return None
        if any(value not in JSON_HEX_DIGITS for value in data[offset + 1 : offset + 5]):
            return None
        offset += 5
    return None


def json_value_end(data: bytes, offset: int) -> int | None:
    offset = skip_json_whitespace(data, offset)
    if offset >= len(data):
        return None
    value = data[offset]
    if value == ord('"'):
        return json_string_end(data, offset)
    if value in b"-0123456789":
        number = JSON_NUMBER.match(data, offset)
        return number.end() if number is not None else None
    for literal in (b"true", b"false", b"null"):
        if data.startswith(literal, offset):
            return offset + len(literal)
    if value == ord("["):
        offset = skip_json_whitespace(data, offset + 1)
        if offset < len(data) and data[offset] == ord("]"):
            return offset + 1
        while True:
            offset = json_value_end(data, offset)
            if offset is None:
                return None
            offset = skip_json_whitespace(data, offset)
            if offset >= len(data):
                return None
            if data[offset] == ord("]"):
                return offset + 1
            if data[offset] != ord(","):
                return None
            offset += 1
    if value != ord("{"):
        return None
    offset = skip_json_whitespace(data, offset + 1)
    if offset < len(data) and data[offset] == ord("}"):
        return offset + 1
    while True:
        offset = json_string_end(data, offset)
        if offset is None:
            return None
        offset = skip_json_whitespace(data, offset)
        if offset >= len(data) or data[offset] != ord(":"):
            return None
        offset = json_value_end(data, offset + 1)
        if offset is None:
            return None
        offset = skip_json_whitespace(data, offset)
        if offset >= len(data):
            return None
        if data[offset] == ord("}"):
            return offset + 1
        if data[offset] != ord(","):
            return None
        offset = skip_json_whitespace(data, offset + 1)


def canonical_compacted_timestamp(line: bytes) -> str:
    compacted = COMPACTED_ROLLOUT_PREFIX.match(line)
    if compacted is None:
        return ""
    try:
        payload_end = json_value_end(line, compacted.end())
    except RecursionError:
        return ""
    if payload_end is None:
        return ""
    closing = skip_json_whitespace(line, payload_end)
    if closing >= len(line) or line[closing] != ord("}"):
        return ""
    if skip_json_whitespace(line, closing + 1) != len(line):
        return ""
    return compacted.group(1).decode("ascii")


def decode_rollout_line(line: bytes) -> dict[str, Any] | None:
    compacted_timestamp = canonical_compacted_timestamp(line)
    if compacted_timestamp:
        return {"type": "compacted", "timestamp": compacted_timestamp}
    try:
        item = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return compact_rollout_event(item) if isinstance(item, dict) else None


def short_text(value: str, max_len: int = 90) -> str:
    text = " ".join(value.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def event_timestamp(item: dict[str, Any]) -> int:
    raw = item.get("timestamp")
    if not isinstance(raw, str):
        return 0
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def event_started_at(payload: dict[str, Any], item: dict[str, Any]) -> int:
    raw = payload.get("started_at")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and re.fullmatch(r"-?[0-9]+", raw):
        try:
            return int(raw)
        except ValueError:
            pass
    return event_timestamp(item)


def tool_arguments(payload: dict[str, Any]) -> str:
    for key in ("input", "arguments"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def command_text(payload: dict[str, Any]) -> str:
    command = payload.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    if isinstance(command, str):
        return command
    raw_input = tool_arguments(payload)
    if not raw_input:
        return ""
    try:
        parsed = json.loads(raw_input)
    except json.JSONDecodeError:
        return raw_input
    if not isinstance(parsed, dict):
        return raw_input
    nested = parsed.get("cmd", parsed.get("command", ""))
    if isinstance(nested, list):
        return " ".join(str(part) for part in nested)
    if isinstance(nested, str):
        return nested
    return ""


def tool_input_session(payload: dict[str, Any]) -> str:
    raw_input = tool_arguments(payload)
    if not raw_input:
        return ""
    match = TOOL_SESSION_PATTERN.search(raw_input)
    return match.group(1) if match else ""


def tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("output")
    if isinstance(output, dict):
        return output
    candidates = [output] if isinstance(output, str) else []
    if isinstance(output, list):
        candidates.extend(
            item.get("text")
            for item in output
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def is_subagent_thread(thread: Thread) -> bool:
    # Plain "cli"/"exec" sources are roots; spawned children carry JSON subagent sources.
    if thread.agent_path or thread.agent_nickname:
        return True
    return "subagent" in thread.source


def agent_label(thread: Thread) -> str:
    if thread.agent_path:
        return thread.agent_path.rstrip("/").rsplit("/", 1)[-1]
    if thread.agent_nickname:
        return thread.agent_nickname
    return "root"


def activity_key(thread: Thread) -> tuple[str, bool, int]:
    return (thread.id, is_subagent_thread(thread), thread.created_at)


def new_rollout_state(
    inode: int,
    key: tuple[str, bool, int] | None,
    token_boundaries: tuple[int, ...] = (),
) -> RolloutStateEntry:
    selected_key = key or ("", False, 0)
    state = RolloutStateEntry(
        inode=inode,
        activity_key=selected_key,
        activity=ActivityState(boundary_found=not selected_key[1]),
    )
    for boundary in dict.fromkeys(token_boundaries):
        state.token_baselines[boundary] = 0
    while len(state.token_baselines) > MAX_TOKEN_BOUNDARIES:
        state.token_baselines.popitem(last=False)
    return state


def update_token_state(state: RolloutStateEntry, item: dict[str, Any]) -> None:
    raw_payload = item.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    if item.get("type") != "event_msg" or payload.get("type") != "token_count":
        return
    state.latest_token = payload
    raw_info = payload.get("info")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    raw_total = info.get("total_token_usage")
    total: dict[str, Any] = raw_total if isinstance(raw_total, dict) else {}
    value = total.get("total_tokens")
    if not isinstance(value, int):
        return
    state.token_count_seen = True
    timestamp = event_timestamp(item)
    state.token_points.append((timestamp, value))
    if len(state.token_points) > MAX_TOKEN_POINTS:
        del state.token_points[: len(state.token_points) - MAX_TOKEN_POINTS]
        state.token_history_dropped = True
    for boundary in state.token_baselines:
        if timestamp < boundary:
            state.token_baselines[boundary] = value


def prune_token_points(state: RolloutStateEntry) -> None:
    if not state.token_baselines or not state.token_points:
        return
    original_count = len(state.token_points)
    oldest_boundary = min(state.token_baselines)
    last_before_index = -1
    for index, point in enumerate(state.token_points):
        if point[0] < oldest_boundary:
            last_before_index = index
    state.token_points = [
        point
        for index, point in enumerate(state.token_points)
        if index == last_before_index or point[0] >= oldest_boundary
    ]
    if len(state.token_points) > MAX_TOKEN_POINTS:
        state.token_points = state.token_points[-MAX_TOKEN_POINTS:]
    if len(state.token_points) < original_count:
        state.token_history_dropped = True


def retain_recent_turn(
    turns: OrderedDict[str, Any],
    turn_id: str,
    value: Any,
    limit: int,
) -> None:
    turns[turn_id] = value
    turns.move_to_end(turn_id)
    while len(turns) > limit:
        turns.popitem(last=False)


def update_activity_state(state: RolloutStateEntry, item: dict[str, Any]) -> None:
    activity = state.activity
    raw_payload = item.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    item_type = str(item.get("type", ""))
    payload_type = str(payload.get("type", ""))
    started_at = event_started_at(payload, item)
    if not activity.boundary_found:
        if (
            payload_type == "task_started"
            and started_at >= state.activity_key[2]
        ):
            activity.boundary_found = True
        else:
            return

    activity.last_event = payload_type or item_type
    if item_type == "compacted" or payload_type == "context_compacted":
        activity.compactions += 1

    if payload_type == "task_started":
        turn_id = str(payload.get("turn_id") or "")
        activity.turns_started += 1
        if turn_id:
            if turn_id in activity.recent_closed_turns:
                activity.recent_closed_turns.move_to_end(turn_id)
                return
            retain_recent_turn(
                activity.started_turns,
                turn_id,
                started_at,
                MAX_OPEN_TURN_IDS,
            )
        return

    if payload_type == "task_complete":
        turn_id = str(payload.get("turn_id") or "")
        activity.turns_completed += 1
        if turn_id:
            activity.started_turns.pop(turn_id, None)
            retain_recent_turn(
                activity.recent_closed_turns,
                turn_id,
                None,
                MAX_RECENT_CLOSED_TURN_IDS,
            )
        return

    if payload_type == "turn_aborted":
        turn_id = str(payload.get("turn_id") or "")
        activity.turns_aborted += 1
        if turn_id:
            activity.started_turns.pop(turn_id, None)
            retain_recent_turn(
                activity.recent_closed_turns,
                turn_id,
                None,
                MAX_RECENT_CLOSED_TURN_IDS,
            )
        return

    if payload_type == "user_message":
        message = payload.get("message")
        if isinstance(message, str) and message:
            activity.last_user_message = short_text(message)
        return

    if payload_type == "agent_message":
        message = payload.get("message")
        if isinstance(message, str) and message:
            activity.last_agent_message = short_text(message)
        return

    if payload_type in {"function_call", "custom_tool_call"}:
        call_id = str(payload.get("call_id") or "")
        name = str(payload.get("name") or "")
        if call_id:
            activity.pending_tools[call_id] = name
            activity.pending_tools.move_to_end(call_id)
            activity.pending_shells.discard(call_id)
            activity.tool_sessions.pop(call_id, None)
            while len(activity.pending_tools) > MAX_ACTIVE_TOOL_CALLS:
                expired_call_id, _ = activity.pending_tools.popitem(last=False)
                activity.pending_shells.discard(expired_call_id)
                activity.tool_sessions.pop(expired_call_id, None)
        activity.tool_calls += 1
        activity.last_tool = name
        session_id = tool_input_session(payload)
        if call_id and session_id:
            activity.tool_sessions[call_id] = session_id
        if name in {"exec", "exec_command"}:
            activity.shell_calls += 1
            if call_id and not session_id:
                activity.pending_shells.add(call_id)
        if name == "apply_patch":
            activity.patch_calls += 1
        command = command_text(payload)
        if command:
            activity.last_command = short_text(command)
        return

    if payload_type in {"function_call_output", "custom_tool_call_output", "patch_apply_end"}:
        call_id = str(payload.get("call_id") or "")
        activity.pending_tools.pop(call_id, None)
        activity.pending_shells.discard(call_id)
        input_session = activity.tool_sessions.pop(call_id, "")
        result = tool_result(payload)
        result_session = str(result.get("session_id") or "")
        if input_session and "exit_code" in result:
            activity.running_shells.pop(input_session, None)
        elif result_session:
            retain_recent_turn(
                activity.running_shells,
                result_session,
                None,
                MAX_RUNNING_SHELLS,
            )
        return

    if payload_type == "exec_command_end":
        call_id = str(payload.get("call_id") or "")
        activity.pending_tools.pop(call_id, None)
        activity.pending_shells.discard(call_id)
        command = command_text(payload)
        if command:
            activity.last_command = short_text(command)


def trim_rollout_state_cache() -> None:
    while len(ROLLOUT_STATE_CACHE) > MAX_ROLLOUT_STATE_CACHE:
        ROLLOUT_STATE_CACHE.popitem(last=False)


def cached_rollout_state(rollout_path: str) -> RolloutStateEntry | None:
    if ACTIVE_ROLLOUT_STATE_SWEEP is not None:
        if len(ACTIVE_ROLLOUT_STATE_SWEEP.encountered) < MAX_ROLLOUT_STATE_CACHE:
            ACTIVE_ROLLOUT_STATE_SWEEP.encountered.setdefault(rollout_path, None)
        entry = ROLLOUT_STATE_CACHE.get(rollout_path)
        if entry is not None:
            return entry
        return ACTIVE_ROLLOUT_STATE_SWEEP.spill.get(rollout_path)
    entry = ROLLOUT_STATE_CACHE.get(rollout_path)
    if entry is not None:
        ROLLOUT_STATE_CACHE.move_to_end(rollout_path)
    return entry


def cache_rollout_state(rollout_path: str, entry: RolloutStateEntry) -> None:
    if rollout_path in ROLLOUT_STATE_CACHE:
        ROLLOUT_STATE_CACHE[rollout_path] = entry
        return
    if ACTIVE_ROLLOUT_STATE_SWEEP is not None:
        if rollout_path in ACTIVE_ROLLOUT_STATE_SWEEP.spill:
            ACTIVE_ROLLOUT_STATE_SWEEP.spill[rollout_path] = entry
            return
        if len(ACTIVE_ROLLOUT_STATE_SWEEP.spill) < MAX_ROLLOUT_SWEEP_CACHE:
            ACTIVE_ROLLOUT_STATE_SWEEP.spill[rollout_path] = entry
        return
    ROLLOUT_STATE_CACHE[rollout_path] = entry
    ROLLOUT_STATE_CACHE.move_to_end(rollout_path)
    trim_rollout_state_cache()


def drop_rollout_state(rollout_path: str) -> None:
    ROLLOUT_STATE_CACHE.pop(rollout_path, None)
    if ACTIVE_ROLLOUT_STATE_SWEEP is not None:
        ACTIVE_ROLLOUT_STATE_SWEEP.spill.pop(rollout_path, None)


@contextmanager
def rollout_state_sweep():
    global ACTIVE_ROLLOUT_STATE_SWEEP
    if ACTIVE_ROLLOUT_STATE_SWEEP is not None:
        yield
        return

    sweep = RolloutStateSweep()
    ACTIVE_ROLLOUT_STATE_SWEEP = sweep
    try:
        yield
    finally:
        retained: OrderedDict[str, RolloutStateEntry] = OrderedDict()
        for rollout_path in sweep.encountered:
            entry = ROLLOUT_STATE_CACHE.get(rollout_path)
            if entry is None:
                entry = sweep.spill.get(rollout_path)
            if entry is not None:
                retained[rollout_path] = entry
            if len(retained) == MAX_ROLLOUT_STATE_CACHE:
                break
        if len(retained) < MAX_ROLLOUT_STATE_CACHE:
            for rollout_path, entry in ROLLOUT_STATE_CACHE.items():
                retained.setdefault(rollout_path, entry)
                if len(retained) == MAX_ROLLOUT_STATE_CACHE:
                    break
        ROLLOUT_STATE_CACHE.clear()
        ROLLOUT_STATE_CACHE.update(retained)
        ACTIVE_ROLLOUT_STATE_SWEEP = None


def feed_pending_rollout_line(pending: PendingRolloutLine, data: bytes) -> None:
    if not data:
        return
    pending.size += len(data)
    if pending.discarded:
        return
    if pending.trailing_only:
        if any(value not in JSON_WHITESPACE for value in data):
            pending.discarded = True
        return
    if pending.compacted is not None:
        pending.compacted.feed(data)
        return
    if pending.selective is not None:
        pending.selective.feed(data)
        return
    if len(pending.buffer) + len(data) <= MAX_BUFFERED_ROLLOUT_LINE_BYTES:
        pending.buffer.extend(data)
        return

    buffered = min(
        len(data),
        MAX_BUFFERED_ROLLOUT_LINE_BYTES - len(pending.buffer),
    )
    pending.buffer.extend(data[:buffered])
    remainder = data[buffered:]
    compacted = COMPACTED_ROLLOUT_PREFIX.match(pending.buffer)
    if compacted is not None:
        scanner = CompactedRecordScanner(compacted.group(1).decode("ascii"))
        scanner.feed(bytes(pending.buffer[compacted.end() :]))
        pending.buffer.clear()
        pending.compacted = scanner
        scanner.feed(remainder)
        return

    if pending.applied and decode_rollout_line(bytes(pending.buffer)) is not None:
        pending.buffer.clear()
        pending.trailing_only = True
        if any(value not in JSON_WHITESPACE for value in remainder):
            pending.discarded = True
        return

    scanner = SelectiveRolloutRecordScanner()
    scanner.feed(bytes(pending.buffer))
    pending.buffer.clear()
    pending.selective = scanner
    scanner.feed(remainder)


def pending_rollout_item(
    pending: PendingRolloutLine,
) -> tuple[bool, dict[str, Any] | None]:
    if pending.discarded:
        return False, None
    if pending.trailing_only:
        return pending.applied, None
    if pending.compacted is not None:
        if not pending.compacted.complete():
            return False, None
        return True, {
            "type": "compacted",
            "timestamp": pending.compacted.timestamp,
        }
    if pending.selective is not None:
        if not pending.selective.complete():
            return False, None
        return True, pending.selective.item()
    if not pending.buffer:
        return False, None
    item = decode_rollout_line(bytes(pending.buffer))
    return item is not None, item


def apply_pending_rollout_line(entry: RolloutStateEntry) -> bool:
    if (
        entry.pending.applied
        and entry.pending.size == entry.pending.applied_size
    ):
        return False
    valid, item = pending_rollout_item(entry.pending)
    if entry.pending.applied:
        if valid:
            entry.pending.applied_size = entry.pending.size
        return not valid
    if not valid or item is None:
        return False
    update_token_state(entry, item)
    update_activity_state(entry, item)
    entry.pending.applied = True
    entry.pending.applied_size = entry.pending.size
    return False


def consume_rollout_stream(stream, entry: RolloutStateEntry) -> bool:
    stream.seek(entry.offset)
    current_offset = entry.offset
    while chunk := stream.read(ROLLOUT_READ_BYTES):
        chunk_offset = 0
        while chunk_offset < len(chunk):
            newline = chunk.find(b"\n", chunk_offset)
            segment_end = len(chunk) if newline < 0 else newline
            segment = chunk[chunk_offset:segment_end]
            feed_pending_rollout_line(entry.pending, segment)
            current_offset += len(segment)
            if newline < 0:
                break
            current_offset += 1
            if apply_pending_rollout_line(entry):
                return True
            entry.pending = PendingRolloutLine()
            chunk_offset = newline + 1
        entry.offset = current_offset
    entry.offset = current_offset
    return bool(entry.pending.size and apply_pending_rollout_line(entry))


def update_rollout_probes(stream, entry: RolloutStateEntry) -> None:
    head_size = min(entry.offset, ROLLOUT_PROBE_BYTES)
    stream.seek(0)
    entry.head_probe = stream.read(head_size)
    entry.probe_offset = max(0, entry.offset - ROLLOUT_PROBE_BYTES)
    stream.seek(entry.probe_offset)
    entry.probe = stream.read(entry.offset - entry.probe_offset)


def read_rollout_state(
    rollout_path: str,
    thread: Thread | None = None,
    token_boundaries: tuple[int, ...] = (),
) -> RolloutStateEntry:
    path = Path(rollout_path)
    requested_key = activity_key(thread) if thread is not None else None
    try:
        stat = path.stat()
    except OSError:
        return new_rollout_state(0, requested_key, token_boundaries)

    entry = cached_rollout_state(rollout_path)
    seeded_boundaries = list(entry.token_baselines) if entry is not None else []
    for boundary in token_boundaries:
        if boundary in seeded_boundaries:
            seeded_boundaries.remove(boundary)
        seeded_boundaries.append(boundary)
    seeded_boundaries = seeded_boundaries[-MAX_TOKEN_BOUNDARIES:]
    token_replay = entry is not None and any(
        boundary not in entry.token_baselines for boundary in token_boundaries
    )
    reset = (
        entry is None
        or entry.inode != stat.st_ino
        or stat.st_size < entry.offset
        or token_replay
        or (
            stat.st_size == entry.offset
            and entry.mtime_ns > 0
            and stat.st_mtime_ns != entry.mtime_ns
        )
    )
    if entry is not None and requested_key is not None and entry.activity_key != requested_key:
        if entry.activity_key == ("", False, 0) and not requested_key[1]:
            entry.activity_key = requested_key
        else:
            reset = True

    growth = entry is not None and stat.st_size > entry.offset
    try:
        with path.open("rb") as stream:
            if not reset and entry is not None:
                if entry.head_probe:
                    stream.seek(0)
                    reset = stream.read(len(entry.head_probe)) != entry.head_probe
                if not reset and entry.probe:
                    stream.seek(entry.probe_offset)
                    reset = stream.read(len(entry.probe)) != entry.probe
            if reset:
                entry = new_rollout_state(
                    stat.st_ino,
                    requested_key,
                    tuple(seeded_boundaries),
                )
                cache_rollout_state(rollout_path, entry)
            assert entry is not None
            if (reset or growth) and consume_rollout_stream(stream, entry):
                reset = True
                entry = new_rollout_state(
                    stat.st_ino,
                    requested_key,
                    tuple(seeded_boundaries),
                )
                cache_rollout_state(rollout_path, entry)
                consume_rollout_stream(stream, entry)
            final_stat = os.fstat(stream.fileno())
            entry.mtime_ns = final_stat.st_mtime_ns
            update_rollout_probes(stream, entry)
    except OSError:
        if reset or growth:
            drop_rollout_state(rollout_path)
            return new_rollout_state(
                stat.st_ino,
                requested_key,
                tuple(seeded_boundaries),
            )
        return entry or new_rollout_state(
            stat.st_ino,
            requested_key,
            tuple(seeded_boundaries),
        )

    cache_rollout_state(rollout_path, entry)
    return entry


def activity_from_state(
    state: ActivityState,
    thread: Thread,
    now: datetime,
    active_window_seconds: int,
) -> RolloutActivity:
    active_starts = [started_at for started_at in state.started_turns.values() if started_at > 0]
    active_turn_seconds = 0
    if active_starts:
        active_turn_seconds = max(0, int(now.timestamp()) - max(active_starts))
    stale = int(now.timestamp()) - thread.updated_at > active_window_seconds
    pending_tools = {} if stale else state.pending_tools
    active_shells = 0 if stale else len(state.pending_shells) + len(state.running_shells)
    if stale:
        active_turn_seconds = 0

    return RolloutActivity(
        turns_started=state.turns_started,
        turns_completed=state.turns_completed,
        turns_aborted=state.turns_aborted,
        compactions=state.compactions,
        tool_calls=state.tool_calls,
        shell_calls=state.shell_calls,
        patch_calls=state.patch_calls,
        active_tools=len(pending_tools),
        active_shells=active_shells,
        active_turn_seconds=active_turn_seconds,
        last_event=state.last_event or "-",
        last_command=state.last_command or "-",
        last_user_message=state.last_user_message or "-",
        last_agent_message=state.last_agent_message or "-",
        active_tool=next(reversed(pending_tools.values()), "-"),
        last_tool=state.last_tool or "-",
    )


def rollout_activity(thread: Thread, now: datetime, active_window_seconds: int = 900) -> RolloutActivity:
    state = read_rollout_state(thread.rollout_path, thread)
    return activity_from_state(state.activity, thread, now, active_window_seconds)


def usage_from_rollout(thread: Thread) -> CodexUsage:
    event = latest_token_count(thread.rollout_path)
    info = event.get("info") if isinstance(event.get("info"), dict) else {}
    last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
    total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
    rate_limits = event.get("rate_limits") if isinstance(event.get("rate_limits"), dict) else {}

    session_total = int(total.get("total_tokens") or thread.tokens_used or 0)
    context_window_value = info.get("model_context_window")
    context_window = int(context_window_value) if isinstance(context_window_value, int) else 0

    return CodexUsage(
        context_window=context_window,
        context_used=int(last.get("input_tokens") or 0),
        turn_total=int(last.get("total_tokens") or 0),
        turn_cached=int(last.get("cached_input_tokens") or 0),
        session_total=session_total,
        session_input=int(total.get("input_tokens") or 0),
        session_cached=int(total.get("cached_input_tokens") or 0),
        session_output=int(total.get("output_tokens") or 0),
        session_reasoning=int(total.get("reasoning_output_tokens") or 0),
        rate_limits=rate_limits,
    )


@lru_cache(maxsize=32)
def model_info(model: str) -> dict[str, Any]:
    cache = read_json(CODEX_HOME / "models_cache.json")
    models = cache.get("models", [])
    if not isinstance(models, list):
        return {}
    for item in models:
        if not isinstance(item, dict):
            continue
        if item.get("slug") == model or item.get("id") == model:
            return item
    return {}


def context_window(model: str) -> int:
    info = model_info(model)
    for key in ("context_window", "max_context_window"):
        value = info.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 0


def display_model(model: str) -> str:
    info = model_info(model)
    value = info.get("display_name")
    return str(value) if value else (model or "unknown")


GIT_INFO_TTL_SECONDS = 5
PR_CACHE_TTL_SECONDS = 90
PR_CACHE_DIR = Path(os.environ.get("CODEX_STATUSLINE_PR_CACHE_DIR", "/tmp/claude"))
PR_REFRESH_PROCESSES: list[subprocess.Popen[Any]] = []


@lru_cache(maxsize=128)
def git_info(cwd: str, fallback_branch: str, bucket: int) -> GitInfo:
    path = cwd or os.getcwd()
    try:
        root = subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.4,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return GitInfo(Path(path).name or "unknown", fallback_branch or "-", "", "", "")

    repo = Path(root).name
    try:
        branch_name = subprocess.check_output(
            ["git", "-C", root, "branch", "--show-current"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.4,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        branch_name = fallback_branch.removesuffix("*")
    try:
        head = subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.4,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        head = ""
    display_branch = branch_name or (head[:8] if head else fallback_branch or "-")

    try:
        dirty = subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.7,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        dirty = ""
    return GitInfo(
        repo,
        f"{display_branch}{'*' if dirty else ''}",
        root,
        branch_name,
        head,
    )


def query_pull_request(repo_root: str, branch: str, head: str) -> dict[str, Any] | None:
    if branch in {"main", "master"}:
        return None
    fields = "number,title,url,state"
    try:
        if branch:
            payload = subprocess.check_output(
                ["gh", "pr", "view", branch, "--json", fields],
                cwd=repo_root,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        elif head:
            repo = subprocess.check_output(
                ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                cwd=repo_root,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            ).strip()
            number = subprocess.check_output(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/commits/{head}/pulls",
                    "--jq",
                    f'map(select(.state == "open" and .head.sha == "{head}")) | first | .number // empty',
                ],
                cwd=repo_root,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            ).strip()
            if not number:
                return None
            payload = subprocess.check_output(
                ["gh", "pr", "view", number, "--json", fields],
                cwd=repo_root,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        else:
            return None
        data = json.loads(payload)
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError):
        return None
    if data.get("state") != "OPEN" or not data.get("number"):
        return None
    return {
        "number": int(data["number"]),
        "title": re.sub(r"[\x00-\x1f\x7f]", " ", str(data.get("title") or "")).strip(),
        "url": str(data.get("url") or ""),
    }


def pr_cache_path(repo_root: str, branch: str, head: str) -> Path:
    ref = branch or head
    digest = hashlib.sha256(f"{repo_root}\0{ref}".encode()).hexdigest()[:16]
    return PR_CACHE_DIR / f"statusline-pr-{digest}.json"


def read_pr_cache(cache_file: Path) -> dict[str, Any] | None:
    data = read_json(cache_file)
    if not data.get("number"):
        return None
    return data


def write_pr_cache(cache_file: Path, pull_request: dict[str, Any] | None) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(pull_request or {}, ensure_ascii=False) + "\n")
        os.replace(temporary, cache_file)
    finally:
        temporary.unlink(missing_ok=True)


def refresh_pr_cache(
    cache_file: Path,
    repo_root: str,
    branch: str,
    head: str,
    lock_fd: int,
) -> None:
    try:
        write_pr_cache(cache_file, query_pull_request(repo_root, branch, head))
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)


def reap_pr_cache_refreshes() -> None:
    PR_REFRESH_PROCESSES[:] = [
        process for process in PR_REFRESH_PROCESSES if process.poll() is None
    ]


def start_pr_cache_refresh(cache_file: Path, repo_root: str, branch: str, head: str) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = Path(f"{cache_file}.lock")
    try:
        descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--refresh-pr-cache",
                "--pr-cache-file",
                str(cache_file),
                "--pr-repo-root",
                repo_root,
                "--pr-branch",
                branch,
                "--pr-head",
                head,
                "--pr-lock-fd",
                str(descriptor),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(descriptor,),
            start_new_session=True,
        )
    except OSError:
        os.close(descriptor)
        return
    os.close(descriptor)
    PR_REFRESH_PROCESSES.append(process)


def pull_request_info(repo_root: str, branch: str, head: str) -> dict[str, Any] | None:
    reap_pr_cache_refreshes()
    if not repo_root or not (branch or head) or branch in {"main", "master"}:
        return None
    cache_file = pr_cache_path(repo_root, branch, head)
    try:
        stale = time.time() - cache_file.stat().st_mtime >= PR_CACHE_TTL_SECONDS
    except OSError:
        stale = True
    if stale:
        start_pr_cache_refresh(cache_file, repo_root, branch, head)
    return read_pr_cache(cache_file)


def color_for_pct(pct: float, p: Palette) -> str:
    if pct >= 90:
        return p.red
    if pct >= 70:
        return p.yellow
    if pct >= 50:
        return p.orange
    return p.green


def build_bar(value: float, goal: float, width: int, p: Palette) -> tuple[str, float]:
    if goal <= 0:
        return f"{p.dim}{'○' * width}{p.reset}", 0.0
    pct = min(100.0, max(0.0, value * 100.0 / goal))
    filled = int(pct * width / 100)
    if pct > 0 and filled == 0:
        filled = 1
    empty = width - filled
    return f"{color_for_pct(pct, p)}{'●' * filled}{p.dim}{'○' * empty}{p.reset}", pct


def build_pct_bar(pct: float, width: int, p: Palette) -> str:
    pct = min(100.0, max(0.0, pct))
    filled = int(pct * width / 100)
    if pct > 0 and filled == 0:
        filled = 1
    empty = width - filled
    return f"{color_for_pct(pct, p)}{'●' * filled}{p.dim}{'○' * empty}{p.reset}"


def format_tokens(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def format_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    minutes = seconds // 60
    hours, mins = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}:{mins:02d}"
    return f"{mins}m"


def format_pct(pct: float) -> str:
    text = f"{pct:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def format_clock(dt: datetime) -> str:
    hour = dt.strftime("%I").lstrip("0") or "0"
    minute = dt.strftime("%M")
    meridiem = dt.strftime("%p").lower()
    zone = dt.tzname() or ""
    return f"{hour}:{minute}{meridiem} {zone}".strip()


def format_reset(timestamp: Any, now: datetime | None = None) -> str:
    try:
        reset_at = datetime.fromtimestamp(int(timestamp)).astimezone()
    except (TypeError, ValueError, OSError):
        return "reset n/a"

    local_now = (now or datetime.now(timezone.utc)).astimezone()
    if reset_at.date() == local_now.date():
        return f"resets {format_clock(reset_at)}"
    if reset_at.year == local_now.year:
        return f"resets {reset_at.strftime('%b').lower()} {reset_at.day} {format_clock(reset_at)}"
    return f"resets {reset_at.strftime('%b').lower()} {reset_at.day} {reset_at.year}"


def limit_display(limit: dict[str, Any], now: datetime | None = None) -> tuple[float, str]:
    """Reset-aware (used_percent, reset_text) for a rate-limit bucket. Once the
    window's resets_at has passed the limit has rolled over, so report 0% and
    "reset" rather than freezing the last-seen percentage against a stale past
    timestamp (the source values only refresh when a session runs a turn)."""
    used = float(limit.get("used_percent") or 0.0)
    resets_at = limit.get("resets_at")
    try:
        reset_at = datetime.fromtimestamp(int(resets_at)).astimezone()
    except (TypeError, ValueError, OSError):
        return used, "reset n/a"
    local_now = (now or datetime.now(timezone.utc)).astimezone()
    if reset_at <= local_now:
        return 0.0, "reset"
    return used, format_reset(resets_at, now)


def sandbox_label(policy: str) -> str:
    if not policy:
        return "-"
    try:
        value = json.loads(policy)
    except json.JSONDecodeError:
        return policy
    if isinstance(value, dict):
        return str(value.get("type", "-"))
    return "-"


def render_goal_row(label: str, value: int, goal: int, width: int, p: Palette, indent: int = 8) -> str:
    bar, pct = build_bar(value, goal, width, p)
    detail = f"{format_tokens(value)}/{format_tokens(goal)}" if goal > 0 else format_tokens(value)
    pct_text = format_pct(pct).ljust(7) if goal > 0 else "goal n/a"
    pct_color = color_for_pct(pct, p)
    return (
        f"{' ' * indent}{p.white}{label:<7}{p.reset} {bar} "
        f"{pct_color}{pct_text}{p.reset} {p.dim}{detail}{p.reset}"
    )


def render_rate_limit_row(label: str, limit: dict[str, Any], width: int, p: Palette, indent: int = 8) -> str:
    used_percent, reset = limit_display(limit)
    bar = build_pct_bar(used_percent, width, p)
    pct_color = color_for_pct(used_percent, p)
    return (
        f"{' ' * indent}{p.white}{label:<7}{p.reset} {bar} "
        f"{pct_color}{format_pct(used_percent).ljust(7)}{p.reset} {p.dim}{reset}{p.reset}"
    )


def rate_limit_window_label(window_minutes: Any, fallback: str) -> str:
    if window_minutes is None:
        return fallback
    if isinstance(window_minutes, bool):
        return "limit"
    if isinstance(window_minutes, int):
        minutes = window_minutes
    elif isinstance(window_minutes, float):
        if not window_minutes.is_integer():
            return "limit"
        minutes = int(window_minutes)
    elif isinstance(window_minutes, str):
        try:
            minutes = int(window_minutes)
        except ValueError:
            return "limit"
    else:
        return "limit"

    if minutes <= 0:
        return "limit"
    if minutes == 300:
        return "5-hour"
    if minutes == 10_080:
        return "weekly"
    if minutes % 1_440 == 0:
        return f"{minutes // 1_440}-day"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return f"{minutes}-minute"


def labeled_rate_limits(rate_limits: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    limits = []
    seen_labels = set()
    for key, fallback_label in (("primary", "5-hour"), ("secondary", "weekly")):
        limit = rate_limits.get(key)
        if not isinstance(limit, dict) or limit.get("used_percent") is None:
            continue
        label = rate_limit_window_label(limit.get("window_minutes"), fallback_label)
        if label in seen_labels:
            continue
        seen_labels.add(label)
        limits.append((label, limit))
    return limits


def snapshot_for_thread(
    thread: Thread,
    tokens: TokenSummary,
    codex_config: dict[str, Any],
    cwd: str,
    now: datetime,
    *,
    prefer_thread_cwd: bool = False,
    include_pull_request: bool = True,
    active_window_seconds: int = 900,
) -> dict[str, Any]:
    model = thread.model or str(codex_config.get("model", ""))
    effort = thread.reasoning_effort or str(codex_config.get("model_reasoning_effort", ""))
    activity = rollout_activity(thread, now, active_window_seconds)
    usage = usage_from_rollout(thread)
    tokens.session = usage.session_total
    repo_cwd = thread.cwd if prefer_thread_cwd else (cwd if paths_related(cwd, thread.cwd) else thread.cwd)
    git = git_info(repo_cwd, thread.git_branch, int(now.timestamp() // GIT_INFO_TTL_SECONDS))
    window = usage.context_window or context_window(model)

    return {
        "thread_id": thread.id,
        "title": thread.title,
        "model": model,
        "model_display": display_model(model),
        "reasoning_effort": effort,
        "context_window": window,
        "account": account_label(),
        "repo": git.repo,
        "branch": git.display_branch,
        "pull_request": (
            pull_request_info(git.root, git.branch_name, git.head)
            if include_pull_request
            else None
        ),
        "cwd": thread.cwd,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
        "session_age_seconds": int(now.timestamp()) - thread.created_at,
        "idle_seconds": int(now.timestamp()) - thread.updated_at,
        "sandbox": sandbox_label(thread.sandbox_policy),
        "approval_mode": thread.approval_mode or "-",
        "tokens": tokens.__dict__,
        "usage": usage.__dict__,
        "activity": activity.__dict__,
        "agent": agent_label(thread),
        "is_subagent": is_subagent_thread(thread),
    }


def descendant_activity_summary(
    descendants: list[Thread],
    now: datetime,
    active_window_seconds: int,
) -> dict[str, int]:
    activities = [
        rollout_activity(thread, now, active_window_seconds)
        for thread in descendants
        if int(now.timestamp()) - thread.updated_at <= active_window_seconds
    ]
    return {
        "total": len(descendants),
        "active": sum(
            1
            for activity in activities
            if activity.active_turn_seconds > 0 or activity.active_tools > 0
        ),
        "active_tools": sum(activity.active_tools for activity in activities),
        "active_shells": sum(activity.active_shells for activity in activities),
    }


def snapshot(args: argparse.Namespace) -> dict[str, Any]:
    with rollout_state_sweep():
        account_label.cache_clear()
        model_info.cache_clear()
        read_codex_config.cache_clear()
        config = parse_shell_config(Path(args.config) if args.config else CONFIG_FILE)
        codex_config = read_codex_config()
        cwd = args.cwd or os.getcwd()
        thread_id = args.thread_id

        if not STATE_DB.exists():
            raise RuntimeError(f"missing Codex state database: {STATE_DB}")

        with closing(sqlite_connect(STATE_DB)) as conn:
            if not thread_id and args.owner_pid_file:
                thread_id = select_owner_thread_id(conn, args.owner_pid_file)
                if not thread_id:
                    raise RuntimeError("no Codex threads found")
            created_after_ms = args.bind_after_ms or args.bind_after * 1000
            thread = select_thread(
                conn,
                thread_id,
                cwd,
                created_after_ms=created_after_ms,
                updated_after_ms=args.bind_updated_after_ms,
            )
            if thread is None:
                raise RuntimeError("no Codex threads found")
            now = datetime.now(timezone.utc)
            tokens = token_summary(conn, thread, now)
            descendants = select_descendant_threads(conn, thread.id)

        data = snapshot_for_thread(thread, tokens, codex_config, cwd, now)
        data["agents"] = descendant_activity_summary(
            descendants,
            now,
            args.active_window,
        )
        goals = {
            "session": int_setting("SESSION_TOKEN_GOAL", config, 1_000_000),
            "today": int_setting("DAILY_TOKEN_GOAL", config, 2_000_000),
            "week": int_setting("WEEKLY_TOKEN_GOAL", config, 10_000_000),
            "lifetime": int_setting("LIFETIME_TOKEN_GOAL", config, 100_000_000),
        }
        data["goals"] = goals
        return data


def all_sessions_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    with rollout_state_sweep():
        account_label.cache_clear()
        model_info.cache_clear()
        read_codex_config.cache_clear()
        codex_config = read_codex_config()
        cwd = args.cwd or os.getcwd()
        if not STATE_DB.exists():
            raise RuntimeError(f"missing Codex state database: {STATE_DB}")

        now = datetime.now(timezone.utc)
        with closing(sqlite_connect(STATE_DB)) as conn:
            if args.top and not args.show_inactive and not args.include_archived:
                threads = select_top_threads(conn, args.sessions)
            else:
                threads = select_threads(conn, args.sessions, args.include_archived)
            if args.top and not args.include_agents:
                threads = [thread for thread in threads if not is_subagent_thread(thread)]
            totals = token_summary(conn, threads[0], now) if threads else TokenSummary(0, 0, 0, 0, 0, 0)
            sessions = []
            for thread in threads:
                thread_tokens = TokenSummary(
                    session=thread.tokens_used,
                    today=totals.today,
                    week=totals.week,
                    lifetime=totals.lifetime,
                    threads_today=totals.threads_today,
                    threads_total=totals.threads_total,
                )
                sessions.append(
                    snapshot_for_thread(
                        thread,
                        thread_tokens,
                        codex_config,
                        cwd,
                        now,
                        prefer_thread_cwd=True,
                        include_pull_request=False,
                        active_window_seconds=args.active_window,
                    )
                )

        newest_rate_limits: dict[str, Any] = {}
        for session in sessions:
            rate_limits = session["usage"].get("rate_limits") or {}
            if rate_limits.get("primary") or rate_limits.get("secondary"):
                newest_rate_limits = rate_limits
                break

        return {
            "account": account_label(),
            "sessions": sessions,
            "session_count": len(sessions),
            "rate_limits": newest_rate_limits,
            "generated_at": int(now.timestamp()),
        }


def render_default(data: dict[str, Any], width: int, p: Palette) -> str:
    usage = data["usage"]
    activity = data["activity"]
    rate_limits = usage.get("rate_limits") or {}
    context = format_tokens(data["context_window"]) if data["context_window"] else "unknown"
    model_bits = [data["model_display"]]
    if data["reasoning_effort"]:
        model_bits.append(data["reasoning_effort"])
    model_bits.append(f"({context} context)")

    lines = [
        "─" * min(width, 120),
        f"        model   {' '.join(model_bits)}",
        f"        time    {format_duration(data['session_age_seconds'])} active · idle {format_duration(data['idle_seconds'])}",
        f"        account {data['account']}",
        f"        repo    {data['repo']} ({data['branch']})",
    ]
    pull_request = data.get("pull_request")
    if pull_request:
        value = short_text(
            f"#{pull_request['number']} {pull_request['title']}",
            max(1, width - 16),
        )
        lines.append(f"        pr      {value}")

    if usage["context_window"] > 0:
        lines.append(render_goal_row("context", usage["context_used"], usage["context_window"], DEFAULT_BAR_WIDTH, p))

    for label, limit in labeled_rate_limits(rate_limits):
        lines.append(render_rate_limit_row(label, limit, DEFAULT_BAR_WIDTH, p))

    lines.extend(
        [
            f"        usage   session {format_tokens(usage['session_total'])} · turn {format_tokens(usage['turn_total'])} · cached {format_tokens(usage['turn_cached'])}",
            f"        output  total {format_tokens(usage['session_output'])} · reasoning {format_tokens(usage['session_reasoning'])}",
            f"        state   {data['sandbox']} · approvals {data['approval_mode']} · plan {rate_limits.get('plan_type', '-')}",
            f"        turns   {activity['turns_completed']}/{activity['turns_started']} done · aborted {activity['turns_aborted']} · compacted {activity['compactions']}",
            f"        tools   {activity['tool_calls']} calls · shell {activity['shell_calls']} · patch {activity['patch_calls']} · active {activity['active_tools']} ({activity['active_shells']} shells)",
            f"        last    {activity['last_event']} · {activity['last_command']}",
            f"        user    {activity['last_user_message']}",
        ]
    )
    if activity["active_turn_seconds"] > 0:
        lines.append(f"        active  current turn running {format_duration(activity['active_turn_seconds'])}")
    return "\n".join(lines)


def render_sigil(data: dict[str, Any], p: Palette) -> str:
    usage = data["usage"]
    activity = data["activity"]
    rate_limits = usage.get("rate_limits") or {}
    limits = labeled_rate_limits(rate_limits)
    limit_label, limit = limits[0] if limits else ("5-hour", {})
    short_limit_label = {"5-hour": "5h", "weekly": "wk"}.get(limit_label, limit_label)
    pct, _ = limit_display(limit)
    bar = build_pct_bar(pct, 10, p)
    effort = f".{data['reasoning_effort']}" if data["reasoning_effort"] else ""
    context = "-"
    if usage["context_window"] > 0:
        context = f"{format_tokens(usage['context_used'])}/{format_tokens(usage['context_window'])}"
    return (
        f"◈ {data['model_display']}{effort} · {data['repo']} ({data['branch']}) · "
        f"{bar} {format_pct(pct)} {short_limit_label} · context {context} · session {format_tokens(usage['session_total'])} · "
        f"tools {activity['active_tools']} · ⏱ {format_duration(data['session_age_seconds'])}"
    )


def render_footer(data: dict[str, Any], width: int, p: Palette) -> str:
    usage = data["usage"]
    activity = data["activity"]
    agents = data.get("agents") or {}
    tokens = data["tokens"]
    rate_limits = usage.get("rate_limits") or {}

    effort_name = data["reasoning_effort"]
    effort = f".{effort_name}" if effort_name else ""

    def solid(color: str) -> Callable[[str], str]:
        return lambda value: f"{color}{value}{p.reset}"

    def model_style(value: str) -> str:
        effort_colors = {
            "low": p.dim,
            "medium": p.orange,
            "high": p.red,
            "xhigh": p.red,
            "max": p.red,
            "ultracode": p.magenta,
        }
        if effort and value.endswith(effort):
            model = value[: -len(effort)]
            return f"{p.blue}{model}{p.reset}{effort_colors.get(effort_name, p.white)}{effort}{p.reset}"
        return f"{p.blue}{value}{p.reset}"

    def repo_style(value: str) -> str:
        repo, separator, branch = value.partition(" (")
        if not separator:
            return f"{p.cyan}{value}{p.reset}"
        if not branch.endswith(")"):
            branch = f"{branch[:-2]}…)" if len(branch) > 1 else ")"
        branch_color = p.orange if "*" in data["branch"] else p.green
        return f"{p.cyan}{repo}{p.reset} {branch_color}({branch}{p.reset}"

    def pr_style(value: str) -> str:
        number, separator, title = value.partition(" ")
        suffix = f" {p.white}{title}{p.reset}" if separator else ""
        return f"{p.cyan}{number}{p.reset}{suffix}"

    def row(
        label: str,
        value: str,
        style: Callable[[str], str] | None = None,
    ) -> str:
        prefix = f"  {label:<7} "
        if width <= len(prefix):
            return f"{p.white}{short_text(f'{label} {value}', max(1, width))}{p.reset}"
        clipped = short_text(value, max(1, width - len(prefix)))
        styled = style(clipped) if style else solid(p.white)(clipped)
        return f"  {p.white}{label:<7}{p.reset} {styled}"

    lines = [
        row("model", f"{data['model_display']}{effort}", model_style),
        row("time", f"⏱ {format_duration(data['session_age_seconds'])}"),
        row("account", data["account"], solid(p.orange)),
        row("repo", f"{data['repo']} ({data['branch']})", repo_style),
    ]
    pull_request = data.get("pull_request")
    if pull_request:
        lines.append(row("pr", f"#{pull_request['number']} {pull_request['title']}", pr_style))
    if usage["context_window"] > 0:
        if width >= 64:
            lines.append(render_goal_row("context", usage["context_used"], usage["context_window"], DEFAULT_BAR_WIDTH, p, 2))
        else:
            context_pct = usage["context_used"] * 100.0 / usage["context_window"]
            lines.append(
                row(
                    "context",
                    f"{format_pct(context_pct)} {format_tokens(usage['context_used'])}/{format_tokens(usage['context_window'])}",
                    solid(color_for_pct(context_pct, p)),
                )
            )
    for label, limit in labeled_rate_limits(rate_limits):
        if width >= 64:
            lines.append(render_rate_limit_row(label, limit, DEFAULT_BAR_WIDTH, p, 2))
        else:
            limit_pct, limit_reset = limit_display(limit)
            lines.append(
                row(
                    label,
                    f"{format_pct(limit_pct)} {limit_reset}",
                    solid(color_for_pct(limit_pct, p)),
                )
            )

    lines.extend(
        [
            row(
                "usage",
                f"today {format_tokens(tokens['today'])} · session {format_tokens(usage['session_total'])} · lifetime {format_tokens(tokens['lifetime'])}",
            ),
            row(
                "agents",
                f"{int(agents.get('active', 0))}/{int(agents.get('total', 0))} running · "
                f"tools {activity['active_tools'] + int(agents.get('active_tools', 0))} · "
                f"shells {activity['active_shells'] + int(agents.get('active_shells', 0))}",
                solid(p.green if int(agents.get("active", 0)) else p.dim),
            ),
            row(
                "mode",
                f"{data.get('sandbox', '-')} · approvals {data.get('approval_mode', '-')}",
                solid(p.dim),
            ),
        ]
    )
    return "\n".join(lines)


def session_marker(session: dict[str, Any]) -> str:
    activity = session["activity"]
    if activity["active_tools"] or activity["active_turn_seconds"]:
        return "*"
    if session["idle_seconds"] < 300:
        return "·"
    return " "


def render_session_row(session: dict[str, Any], p: Palette, details: bool) -> list[str]:
    usage = session["usage"]
    activity = session["activity"]
    context = "-"
    if usage["context_window"] > 0:
        context_pct = usage["context_used"] * 100.0 / usage["context_window"]
        context = f"{build_pct_bar(context_pct, 8, p)} {format_pct(context_pct):>6}"
    effort = f".{session['reasoning_effort']}" if session["reasoning_effort"] else ""
    marker = session_marker(session)
    repo = short_text(session["repo"], 16)
    branch = short_text(session["branch"], 14)
    model = short_text(f"{session['model_display']}{effort}", 16)
    active = f"active {format_duration(activity['active_turn_seconds'])}" if activity["active_turn_seconds"] else f"idle {format_duration(session['idle_seconds'])}"
    line = (
        f"        {marker} {repo:<16} {branch:<14} {model:<16} "
        f"ctx {context:<17} ses {format_tokens(usage['session_total']):>7} "
        f"turn {format_tokens(usage['turn_total']):>7} "
        f"tool {activity['active_tools']}/{activity['active_shells']} · {active}"
    )
    if not details:
        return [line]
    return [
        line,
        f"          ask  {activity['last_user_message']}",
        f"          last {activity['last_event']} · {activity['last_command']}",
    ]


def render_all_sessions(data: dict[str, Any], width: int, p: Palette, details: bool = False) -> str:
    sessions = data["sessions"]
    rate_limits = data.get("rate_limits") or {}
    total_tokens = sum(session["usage"]["session_total"] for session in sessions)
    active_tools = sum(session["activity"]["active_tools"] for session in sessions)
    active_shells = sum(session["activity"]["active_shells"] for session in sessions)
    active_turns = sum(1 for session in sessions if session["activity"]["active_turn_seconds"] > 0)

    lines = [
        "─" * min(width, 120),
        f"        account  {data['account']}",
        f"        sessions {len(sessions)} shown · active {active_turns} · tools {active_tools} · shells {active_shells} · tokens {format_tokens(total_tokens)}",
    ]
    for label, limit in labeled_rate_limits(rate_limits):
        lines.append(render_rate_limit_row(label, limit, DEFAULT_BAR_WIDTH, p))
    lines.append("        ·")
    lines.append("        # repo             branch         model            context           session    turn    tools state")

    for session in sessions:
        lines.extend(render_session_row(session, p, details))
    return "\n".join(lines)


def top_status(activity: dict[str, Any], idle_seconds: int) -> str:
    if activity["active_tool"] in {"request_user_input", "mcp_elicitation"}:
        return "WAIT"
    if activity["active_shells"]:
        return "SHELL"
    if activity["active_tools"]:
        return "TOOL"
    if activity["active_turn_seconds"]:
        return "RUN"
    closed_turns = activity["turns_completed"] + activity["turns_aborted"]
    if activity["turns_started"] and closed_turns >= activity["turns_started"]:
        return "DONE"
    if idle_seconds < 300:
        return "IDLE"
    return "DONE"


def filter_top_sessions(
    sessions: list[dict[str, Any]],
    *,
    active_only: bool,
    hide_inactive: bool,
) -> list[dict[str, Any]]:
    active_statuses = {"WAIT", "SHELL", "TOOL", "RUN", "IDLE"}
    if active_only:
        return [
            session
            for session in sessions
            if top_status(session["activity"], session["idle_seconds"]) in active_statuses
        ]
    if hide_inactive:
        return [
            session
            for session in sessions
            if (
                top_status(session["activity"], session["idle_seconds"]) in active_statuses
                or session["is_subagent"]
            )
        ]
    return sessions


def top_sort_key(session: dict[str, Any], sort: str) -> tuple[Any, ...]:
    usage = session["usage"]
    activity = session["activity"]
    status = top_status(activity, session["idle_seconds"])
    status_rank = {"WAIT": 0, "SHELL": 1, "TOOL": 2, "RUN": 3, "IDLE": 4, "DONE": 5}.get(status, 9)
    context_pct = 0.0
    if usage["context_window"] > 0:
        context_pct = usage["context_used"] * 100.0 / usage["context_window"]
    if sort == "context":
        return (-context_pct, status_rank, session["idle_seconds"])
    if sort == "tokens":
        return (-usage["session_total"], status_rank, session["idle_seconds"])
    if sort == "idle":
        return (session["idle_seconds"], status_rank)
    return (status_rank, session["idle_seconds"], -usage["turn_total"])


def render_top(data: dict[str, Any], args: argparse.Namespace, p: Palette) -> str:
    width = args.width
    sessions = sorted(data["sessions"], key=lambda session: top_sort_key(session, args.sort))
    sessions = filter_top_sessions(
        sessions,
        active_only=args.active_only,
        hide_inactive=args.top and not args.show_inactive,
    )
    total_sessions = len(sessions)
    total_tokens = sum(session["usage"]["session_total"] for session in sessions)
    active_turns = sum(1 for session in sessions if session["activity"]["active_turn_seconds"] > 0)
    active_tools = sum(session["activity"]["active_tools"] for session in sessions)
    active_shells = sum(session["activity"]["active_shells"] for session in sessions)
    max_rows = args.rows
    if max_rows <= 0:
        max_rows = max(4, terminal_size().lines - 8)
    hidden = max(0, len(sessions) - max_rows)
    sessions = sessions[:max_rows]
    rate_limits = data.get("rate_limits") or {}
    limits = labeled_rate_limits(rate_limits)

    now_text = datetime.now().astimezone().strftime("%H:%M:%S %Z")
    wide = width >= 132
    medium = width >= 78
    rate_width = 24 if width >= 72 else 10

    if width >= 100:
        summary = (
            f"{p.cyan}codex-top{p.reset} {now_text}  account {data['account']}  "
            f"sessions {len(sessions)}/{total_sessions}  active {active_turns}  "
            f"tools {active_tools}  shells {active_shells}  tokens {format_tokens(total_tokens)}"
        )
    else:
        summary = (
            f"{p.cyan}codex-top{p.reset} {now_text}  sessions {len(sessions)}/{total_sessions}  "
            f"active {active_turns}  tokens {format_tokens(total_tokens)}"
        )

    if wide:
        columns = f"{'ST':<5} {'AGE':>6} {'IDLE':>6} {'CTX':<15} {'SESSION':>8} {'TURN':>7} {'OUT':>6} {'RSN':>6} {'ACTION':<12} {'AGENT':<18} {'REPO':<14} {'MODEL':<14}"
    elif medium:
        columns = f"{'ST':<5} {'CTX':<15} {'SESSION':>8} {'ACTION':<12} {'AGENT':<18} {'REPO':<14}"
    else:
        columns = f"{'ST':<5} {'AGENT':<14} {'CTX':>6} {'SESSION':>8} {'ACTION':<10}"

    lines = [summary]
    for label, limit in limits:
        short_label = {"5-hour": "5h", "weekly": "wk"}.get(label, label)
        used_percent, limit_reset = limit_display(limit)
        lines.append(
            f"{short_label} [{build_pct_bar(used_percent, rate_width, p)}] "
            f"{format_pct(used_percent):>6} {limit_reset}"
        )
    lines.extend(["─" * min(width, 140), columns])

    for session in sessions:
        usage = session["usage"]
        activity = session["activity"]
        ctx_pct = 0.0
        if usage["context_window"] > 0:
            ctx_pct = usage["context_used"] * 100.0 / usage["context_window"]
        status = top_status(activity, session["idle_seconds"])
        status_color = p.green
        if status in {"SHELL", "TOOL", "RUN"}:
            status_color = p.orange
        elif status == "WAIT":
            status_color = p.yellow
        elif status == "DONE":
            status_color = p.dim
        model = short_text(f"{session['model_display']}.{session['reasoning_effort']}" if session["reasoning_effort"] else session["model_display"], 14)
        repo = short_text(session["repo"], 14)
        action = short_text(activity["active_tool"] if activity["active_tool"] != "-" else activity["last_tool"], 12)
        agent = short_text(session["agent"], 18)
        ctx_bar = build_pct_bar(ctx_pct, 8, p)
        ctx = f"{ctx_bar} {format_pct(ctx_pct):>6}"
        if wide:
            line = (
                f"{status_color}{status:<5}{p.reset} "
                f"{format_duration(session['session_age_seconds']):>6} "
                f"{format_duration(session['idle_seconds']):>6} "
                f"{ctx:<15} "
                f"{format_tokens(usage['session_total']):>8} "
                f"{format_tokens(usage['turn_total']):>7} "
                f"{format_tokens(usage['session_output']):>6} "
                f"{format_tokens(usage['session_reasoning']):>6} "
                f"{action:<12} "
                f"{agent:<18} "
                f"{repo:<14} "
                f"{model:<14}"
            )
        elif medium:
            line = (
                f"{status_color}{status:<5}{p.reset} "
                f"{ctx:<15} "
                f"{format_tokens(usage['session_total']):>8} "
                f"{action:<12} "
                f"{agent:<18} "
                f"{repo:<14}"
            )
        else:
            line = (
                f"{status_color}{status:<5}{p.reset} "
                f"{short_text(agent, 14):<14} "
                f"{format_pct(ctx_pct):>6} "
                f"{format_tokens(usage['session_total']):>8} "
                f"{short_text(action, 10):<10}"
            )
        lines.append(line)

    if hidden:
        lines.append(f"{p.dim}… {hidden} more sessions hidden; use --rows or --sessions to show more{p.reset}")
    filters = []
    if args.top and not args.show_inactive:
        filters.append("active")
    if args.top and not args.include_agents:
        filters.append("parents")
    filter_text = ",".join(filters) if filters else "none"
    lines.append(f"{p.dim}sort={args.sort} rows={max_rows} filters={filter_text} interval={args.watch or 0:g}s · Ctrl-C to stop live mode{p.reset}")
    return "\n".join(lines)


def render(data: dict[str, Any], args: argparse.Namespace, p: Palette) -> str:
    if args.json:
        return json.dumps(data, indent=2, sort_keys=True)
    if args.top:
        return render_top(data, args, p)
    if args.all:
        return render_all_sessions(data, args.width, p, args.details)
    if args.footer:
        return render_footer(data, args.width, p)
    fmt = args.format
    if fmt == "sigil":
        return render_sigil(data, p)
    return render_default(data, args.width, p)


def watch(args: argparse.Namespace, p: Palette) -> int:
    if args.footer:
        print("\033[?1049h", end="")
        sys.stdout.flush()
    try:
        return watch_loop(args, p)
    finally:
        if args.footer:
            print("\033[?1049l", end="")
            sys.stdout.flush()


def watch_loop(args: argparse.Namespace, p: Palette) -> int:
    wal_attempt = 0.0
    while True:
        latest_activity_ms = 0
        try:
            if args.owner_pid_file and not owner_alive(args.owner_pid_file):
                return 0
            if args.dynamic_width:
                args.width = terminal_size().columns
            data = all_sessions_snapshot(args) if args.all or args.top else snapshot(args)
            latest_activity_ms = snapshot_activity_ms(data, bool(args.all or args.top))
            if (
                (args.bind_after or args.bind_after_ms or args.bind_updated_after_ms)
                and not args.owner_pid_file
                and not args.thread_id
                and not args.all
                and not args.top
            ):
                args.thread_id = data["thread_id"]
            body = render(data, args, p)
            timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z")
            print("\033[2J\033[H", end="")
            print(body)
            if not args.footer:
                print(f"\n        refresh {timestamp} · Ctrl-C to stop")
            sys.stdout.flush()
        except KeyboardInterrupt:
            print()
            return 0
        except Exception as exc:
            print("\033[2J\033[H", end="")
            binding = args.owner_pid_file or args.bind_after or args.bind_after_ms or args.bind_updated_after_ms
            if args.footer and binding and str(exc) == "no Codex threads found":
                print("  status  Waiting for this Codex session…")
            else:
                print(f"Codex status unavailable: {exc}")
            sys.stdout.flush()
        wal_attempt = maybe_checkpoint_wal(
            STATE_DB, last_attempt=wal_attempt, now=time.monotonic()
        )
        try:
            sleep_s = next_sleep_seconds(args.watch, latest_activity_ms, int(time.time() * 1000))
            time.sleep(floor_multi_session_sleep(sleep_s, bool(args.all or args.top)))
        except KeyboardInterrupt:
            print()
            return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", default="")
    config_args, _ = config_parser.parse_known_args(argv)
    config_path = Path(config_args.config) if config_args.config else CONFIG_FILE
    config = parse_shell_config(config_path)
    format_default = os.environ.get("CODEX_STATUSLINE_FORMAT") or config.get(
        "CODEX_STATUSLINE_FORMAT", "default"
    )

    parser = argparse.ArgumentParser(description="Render a Codex CLI statusline from local state.")
    parser.add_argument("--format", choices=("default", "sigil"), default=format_default)
    parser.add_argument("--json", action="store_true", help="print the raw computed snapshot")
    parser.add_argument("--thread-id", default="", help="Codex thread id to render; defaults to nearest recent thread")
    parser.add_argument("--bind-after", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--bind-after-ms", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--bind-updated-after-ms", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--owner-pid-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--cwd", default="", help="cwd used to choose the nearest recent Codex thread")
    parser.add_argument("--config", default="", help="config file path; defaults to ~/.codex/statusline.conf")
    parser.add_argument("--width", type=int, default=default_width())
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color")
    parser.add_argument("--watch", type=float, default=0.0, metavar="SECONDS", help="refresh in place every N seconds")
    parser.add_argument("--all", action="store_true", help="show a single dashboard for all recent Codex sessions")
    parser.add_argument("--top", action="store_true", help="show a btop/nvitop-style all-session monitor")
    parser.add_argument("--footer", action="store_true", help="show the compact dashboard used by the codex-statusline launcher")
    parser.add_argument("--sessions", type=int, default=30, help="number of sessions to load with --all/--top")
    parser.add_argument("--include-archived", action="store_true", help="include archived sessions in --all")
    parser.add_argument("--details", action="store_true", help="include last prompt and command under each session in --all")
    parser.add_argument("--active-window", type=int, default=900, help="seconds before an unclosed turn is treated as stale")
    parser.add_argument("--rows", type=int, default=0, help="visible row count for --top; defaults to terminal height")
    parser.add_argument("--active-only", action="store_true", help="hide completed/stale sessions in --top")
    parser.add_argument("--show-inactive", action="store_true", help="show completed/stale sessions in --top")
    parser.add_argument("--include-agents", action="store_true", help="include subagent sessions in --top")
    parser.add_argument("--sort", choices=("active", "context", "tokens", "idle"), default="active", help="sort mode for --top")
    parser.add_argument("--refresh-pr-cache", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pr-cache-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--pr-repo-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--pr-branch", default="", help=argparse.SUPPRESS)
    parser.add_argument("--pr-head", default="", help=argparse.SUPPRESS)
    parser.add_argument("--pr-lock-fd", type=int, default=-1, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (
        not args.thread_id
        and not args.owner_pid_file
        and not args.bind_after
        and not args.bind_after_ms
        and not args.bind_updated_after_ms
    ):
        args.thread_id = os.environ.get("CODEX_THREAD_ID") or config.get("CODEX_THREAD_ID", "")
    args.dynamic_width = not any(arg == "--width" or arg.startswith("--width=") for arg in argv)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.refresh_pr_cache:
        refresh_pr_cache(
            Path(args.pr_cache_file),
            args.pr_repo_root,
            args.pr_branch,
            args.pr_head,
            args.pr_lock_fd,
        )
        return 0
    color_enabled = not args.no_color and not os.environ.get("NO_COLOR")
    if args.watch > 0:
        return watch(args, Palette(color_enabled))
    try:
        data = all_sessions_snapshot(args) if args.all or args.top else snapshot(args)
        print(render(data, args, Palette(color_enabled)))
    except Exception as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Codex status unavailable: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

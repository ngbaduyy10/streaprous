"""Payload serialization: JSON for structured messages, a binary layout for file chunks."""

from __future__ import annotations

import json
import re
import struct
import uuid
from typing import Any

from streamlink.protocol.constants import FILE_HEADER_SIZE, USERNAME_PATTERN

_CHUNK_HEADER = struct.Struct("!16sQ")
_USERNAME_RE = re.compile(USERNAME_PATTERN)
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def encode_json(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_json(data: bytes) -> dict[str, Any]:
    """Decode a JSON object payload. Raises ValueError on malformed input."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON payload: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("JSON payload must be an object")
    return obj


def pack_chunk(transfer_id: str, offset: int, data: bytes) -> bytes:
    return _CHUNK_HEADER.pack(uuid.UUID(hex=transfer_id).bytes, offset) + data


def unpack_chunk(payload: bytes) -> tuple[str, int, bytes]:
    if len(payload) < FILE_HEADER_SIZE:
        raise ValueError("file chunk too short")
    raw_id, offset = _CHUNK_HEADER.unpack_from(payload)
    return raw_id.hex(), offset, payload[FILE_HEADER_SIZE:]


def new_transfer_id() -> str:
    return uuid.uuid4().hex


def valid_username(name: Any) -> bool:
    return isinstance(name, str) and bool(_USERNAME_RE.match(name))


def valid_transfer_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX32_RE.match(value))


def valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64_RE.match(value))


def safe_filename(name: Any, default: str = "file") -> str:
    """Reduce an untrusted file name to a safe basename."""
    if not isinstance(name, str):
        return default
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
    return name[:200] or default

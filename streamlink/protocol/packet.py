"""Binary packet header codec and TCP framing.

Header layout (big-endian, 27 bytes)::

    magic(2) version(1) type(1) flags(1) session_id(4) stream_id(4)
    seq(4) ack(4) sack_bitmap(4) payload_len(2)
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from streamlink.protocol.constants import HEADER_SIZE, MAGIC, MAX_TCP_PAYLOAD, VERSION

_HEADER = struct.Struct("!HBBBIIIIIH")
assert _HEADER.size == HEADER_SIZE


class PacketError(ValueError):
    """Raised when bytes on the wire do not form a valid StreamLink packet."""


@dataclass
class Header:
    """The fixed 27-byte header that starts every StreamLink packet (see docs/PROTOCOL.md)."""

    type: int
    flags: int = 0
    session_id: int = 0
    stream_id: int = 0
    seq: int = 0
    ack: int = 0
    sack: int = 0
    payload_len: int = 0

    def pack(self) -> bytes:
        return _HEADER.pack(
            MAGIC,
            VERSION,
            self.type,
            self.flags,
            self.session_id,
            self.stream_id,
            self.seq,
            self.ack,
            self.sack,
            self.payload_len,
        )

    @classmethod
    def unpack(cls, data: bytes) -> Header:
        if len(data) < HEADER_SIZE:
            raise PacketError(f"packet too short ({len(data)} bytes)")
        magic, version, ptype, flags, sid, stream, seq, ack, sack, plen = _HEADER.unpack_from(data)
        if magic != MAGIC:
            raise PacketError(f"bad magic 0x{magic:04x}")
        if version != VERSION:
            raise PacketError(f"unsupported version {version}")
        return cls(ptype, flags, sid, stream, seq, ack, sack, plen)


def encode(header: Header, payload: bytes = b"") -> bytes:
    """Serialize a header and payload. ``header.payload_len`` is set from the payload."""
    if len(payload) > 0xFFFF:
        raise PacketError("payload too large")
    header.payload_len = len(payload)
    return header.pack() + payload


def decode(data: bytes) -> tuple[Header, bytes, bytes]:
    """Parse a datagram into ``(header, raw_header_bytes, payload)``.

    The raw header bytes are returned because they are the AEAD associated data.
    """
    header = Header.unpack(data)
    end = HEADER_SIZE + header.payload_len
    if len(data) != end:
        raise PacketError(
            f"length mismatch: header says {header.payload_len}, got {len(data) - HEADER_SIZE}"
        )
    return header, data[:HEADER_SIZE], data[HEADER_SIZE:end]


async def read_frame(reader: asyncio.StreamReader) -> tuple[Header, bytes, bytes]:
    """Read one framed packet from a TCP stream."""
    raw = await reader.readexactly(HEADER_SIZE)
    header = Header.unpack(raw)
    if header.payload_len > MAX_TCP_PAYLOAD:
        raise PacketError("TCP frame too large")
    payload = await reader.readexactly(header.payload_len)
    return header, raw, payload


async def write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write an already encoded packet to a TCP stream."""
    writer.write(data)
    await writer.drain()

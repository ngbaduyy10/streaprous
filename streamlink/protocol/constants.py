"""Protocol constants shared by the client and the server. See docs/PROTOCOL.md."""

from __future__ import annotations

from enum import IntEnum, IntFlag

MAGIC = 0x534C  # "SL"
VERSION = 1

HEADER_SIZE = 27
MAX_DATAGRAM = 1200
NONCE_SIZE = 12
TAG_SIZE = 16
AEAD_OVERHEAD = NONCE_SIZE + TAG_SIZE
MAX_PLAINTEXT = MAX_DATAGRAM - HEADER_SIZE - AEAD_OVERHEAD
MAX_FRAGMENT = 1100

FILE_CHUNK_SIZE = 1024
FILE_HEADER_SIZE = 24  # transfer id (16) + offset (8)
MAX_FILE_SIZE = 2 * 1024**3

MAX_TEXT_LENGTH = 4000
MAX_TCP_PAYLOAD = 64 * 1024

DEFAULT_TCP_PORT = 9000
DEFAULT_UDP_PORT = 9001

USERNAME_PATTERN = r"^[A-Za-z0-9_.-]{1,32}$"


class PacketType(IntEnum):
    """Value of the ``type`` header field. Types >= RELIABLE_MIN go through RUDP."""

    # Transport control, unreliable (UDP)
    ACK = 0x01
    PING = 0x02
    PONG = 0x03
    BYE = 0x04
    # Handshake (TCP)
    HELLO = 0x10
    AUTH = 0x11
    AUTH_OK = 0x12
    AUTH_FAIL = 0x13
    # Application messages, reliable (RUDP streams)
    LIST = 0x20
    PRESENCE = 0x21
    MSG_ALL = 0x22
    MSG_DM = 0x23
    FILE_OFFER = 0x30
    FILE_ACCEPT = 0x31
    FILE_REJECT = 0x32
    FILE_CHUNK = 0x33
    FILE_DONE = 0x34
    FILE_PROGRESS = 0x35
    FILE_CANCEL = 0x36
    ERROR = 0x3F


RELIABLE_MIN = 0x20


def is_reliable(ptype: int) -> bool:
    """Return True for packet types carried on reliable RUDP streams."""
    return ptype >= RELIABLE_MIN


class Flags(IntFlag):
    """Bits of the ``flags`` header field."""

    NONE = 0
    FIN = 0x01
    FRAG = 0x02
    ACK = 0x04
    RETX = 0x08
    ENC = 0x10

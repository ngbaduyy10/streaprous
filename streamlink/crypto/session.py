"""Session key agreement and authenticated encryption of packet payloads.

Both peers generate an ephemeral X25519 key pair during HELLO. The shared secret is expanded
with HKDF-SHA256 into one AES-256-GCM key per direction. Nonces are a per-direction counter,
so a key never sees the same nonce twice (retransmissions are re-encrypted with a fresh nonce).
"""

from __future__ import annotations

import base64
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from streamlink.protocol.constants import AEAD_OVERHEAD, NONCE_SIZE, Flags
from streamlink.protocol.packet import Header

_NONCE = struct.Struct("!IQ")
_INFO_PREFIX = b"streamlink/1"


class CryptoError(Exception):
    """Raised when a payload cannot be authenticated or decrypted."""


class KeyPair:
    """Ephemeral X25519 key pair."""

    def __init__(self) -> None:
        self._private = X25519PrivateKey.generate()
        self.public = self._private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @property
    def public_b64(self) -> str:
        return base64.b64encode(self.public).decode("ascii")

    def exchange(self, peer_public: bytes) -> bytes:
        try:
            peer = X25519PublicKey.from_public_bytes(peer_public)
            return self._private.exchange(peer)
        except ValueError as exc:
            raise CryptoError(f"invalid peer public key: {exc}") from exc


def decode_public(value: object) -> bytes:
    if not isinstance(value, str):
        raise CryptoError("public key must be a base64 string")
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise CryptoError("public key is not valid base64") from exc
    if len(raw) != 32:
        raise CryptoError("public key must be 32 bytes")
    return raw


def _derive(
    shared: bytes, session_id: int, client_pub: bytes, server_pub: bytes
) -> tuple[bytes, bytes]:
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=session_id.to_bytes(4, "big"),
        info=_INFO_PREFIX + client_pub + server_pub,
    ).derive(shared)
    return material[:32], material[32:]


class PacketCipher:
    """AES-256-GCM with separate send/receive keys and counter-based nonces."""

    overhead = AEAD_OVERHEAD

    def __init__(self, send_key: bytes, recv_key: bytes) -> None:
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._counter = 0

    def seal(self, aad: bytes, plaintext: bytes) -> bytes:
        """Encrypt ``plaintext``; returns ``nonce || ciphertext || tag``."""
        self._counter += 1
        nonce = _NONCE.pack(0, self._counter)
        return nonce + self._send.encrypt(nonce, plaintext, aad)

    def open(self, aad: bytes, blob: bytes) -> bytes:
        """Authenticate and decrypt ``nonce || ciphertext || tag``."""
        if len(blob) < AEAD_OVERHEAD:
            raise CryptoError("ciphertext too short")
        try:
            return self._recv.decrypt(blob[:NONCE_SIZE], blob[NONCE_SIZE:], aad)
        except InvalidTag as exc:
            raise CryptoError("authentication failed") from exc


def seal_packet(cipher: PacketCipher, header: Header, plaintext: bytes) -> bytes:
    """Build a full packet whose payload is encrypted with the header as associated data."""
    header.flags |= Flags.ENC
    header.payload_len = len(plaintext) + AEAD_OVERHEAD
    raw = header.pack()
    return raw + cipher.seal(raw, plaintext)


def client_cipher(keys: KeyPair, server_pub: bytes, session_id: int) -> PacketCipher:
    c2s, s2c = _derive(keys.exchange(server_pub), session_id, keys.public, server_pub)
    return PacketCipher(send_key=c2s, recv_key=s2c)


def server_cipher(keys: KeyPair, client_pub: bytes, session_id: int) -> PacketCipher:
    c2s, s2c = _derive(keys.exchange(client_pub), session_id, client_pub, keys.public)
    return PacketCipher(send_key=s2c, recv_key=c2s)

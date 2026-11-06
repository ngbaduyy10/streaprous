import pytest

from streamlink.crypto.session import (
    CryptoError,
    KeyPair,
    client_cipher,
    seal_packet,
    server_cipher,
)
from streamlink.protocol.constants import (
    HEADER_SIZE,
    MAX_DATAGRAM,
    MAX_FRAGMENT,
    MAX_PLAINTEXT,
    Flags,
    PacketType,
)
from streamlink.protocol.messages import (
    decode_json,
    encode_json,
    new_transfer_id,
    pack_chunk,
    safe_filename,
    unpack_chunk,
    valid_username,
)
from streamlink.protocol.packet import Header, PacketError, decode, encode


def test_header_roundtrip():
    header = Header(PacketType.MSG_DM, Flags.FIN, 0xDEADBEEF, 7, 123456, 99, 0b1011)
    data = encode(header, b"payload")
    assert len(data) == HEADER_SIZE + 7
    parsed, raw, payload = decode(data)
    assert parsed == header
    assert raw == data[:HEADER_SIZE]
    assert payload == b"payload"


@pytest.mark.parametrize(
    "data",
    [b"", b"SL", b"XX" + bytes(25), encode(Header(PacketType.PING), b"abc")[:-1]],
)
def test_decode_rejects_malformed(data):
    with pytest.raises(PacketError):
        decode(data)


def test_decode_rejects_wrong_version():
    data = bytearray(encode(Header(PacketType.PING)))
    data[2] = 9
    with pytest.raises(PacketError):
        decode(bytes(data))


def test_fragment_fits_in_datagram():
    assert MAX_FRAGMENT <= MAX_PLAINTEXT
    assert HEADER_SIZE + 28 + MAX_FRAGMENT <= MAX_DATAGRAM


def test_chunk_roundtrip():
    tid = new_transfer_id()
    assert unpack_chunk(pack_chunk(tid, 2**40, b"data")) == (tid, 2**40, b"data")


def test_json_helpers():
    assert decode_json(encode_json({"text": "xin chào"})) == {"text": "xin chào"}
    with pytest.raises(ValueError):
        decode_json(b"[1, 2]")
    with pytest.raises(ValueError):
        decode_json(b"\xff")


def test_validation_helpers():
    assert valid_username("duy_01")
    assert not valid_username("bad name")
    assert not valid_username("x" * 33)
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\temp\\a<b>.txt") == "a_b_.txt"
    assert safe_filename("") == "file"


def test_key_agreement_and_aead():
    client_keys, server_keys = KeyPair(), KeyPair()
    c = client_cipher(client_keys, server_keys.public, 42)
    s = server_cipher(server_keys, client_keys.public, 42)
    packet = seal_packet(c, Header(PacketType.MSG_ALL, session_id=42), b"secret text")
    header, raw, payload = decode(packet)
    assert header.flags & Flags.ENC
    assert b"secret text" not in packet
    assert s.open(raw, payload) == b"secret text"

    reply = seal_packet(s, Header(PacketType.MSG_ALL, session_id=42), b"answer")
    _, raw, payload = decode(reply)
    assert c.open(raw, payload) == b"answer"


def test_aead_detects_tampering():
    client_keys, server_keys = KeyPair(), KeyPair()
    c = client_cipher(client_keys, server_keys.public, 1)
    s = server_cipher(server_keys, client_keys.public, 1)
    packet = bytearray(seal_packet(c, Header(PacketType.MSG_ALL, session_id=1, seq=5), b"hello"))
    packet[15] ^= 0x01  # flip a bit of the seq field (authenticated as AAD)
    _, raw, payload = decode(bytes(packet))
    with pytest.raises(CryptoError):
        s.open(raw, payload)


def test_nonces_never_repeat():
    client_keys, server_keys = KeyPair(), KeyPair()
    c = client_cipher(client_keys, server_keys.public, 1)
    nonces = {c.seal(b"", b"x")[:12] for _ in range(1000)}
    assert len(nonces) == 1000


def test_wrong_session_keys_fail():
    a, b, other = KeyPair(), KeyPair(), KeyPair()
    c = client_cipher(a, b.public, 1)
    wrong = server_cipher(b, other.public, 1)
    _, raw, payload = decode(seal_packet(c, Header(PacketType.PING, session_id=1), b"x"))
    with pytest.raises(CryptoError):
        wrong.open(raw, payload)

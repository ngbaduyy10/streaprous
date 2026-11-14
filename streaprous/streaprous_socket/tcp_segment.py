"""
StreapRous TCP segment helpers
Parses the IP datagrams read from the raw socket and handles 32-bit
sequence numbers. Checksums are not checked.
"""

import socket
import struct
from collections import namedtuple

FIN, SYN, RST, PSH, ACK = 0x01, 0x02, 0x04, 0x08, 0x10
SEQ_MOD = 2 ** 32

Segment = namedtuple('Segment', ['src_ip', 'dst_ip', 'src_port', 'dst_port',
                                 'seq', 'ack', 'flags', 'window', 'payload'])


def parse_tcp_packet(packet):
    """Reads one IP datagram and returns its TCP segment.

    Args:
         packet  IP datagram bytes from the raw socket

    Returns:
         Segment with raw 32-bit seq and ack, or None when the bytes are
         not a complete IPv4 TCP packet
    """
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    ihl = (packet[0] & 0x0f) * 4
    if ihl < 20 or packet[9] != socket.IPPROTO_TCP:
        return None
    total_len = struct.unpack('!H', packet[2:4])[0]
    if total_len < len(packet):
        packet = packet[:total_len]
    tcp = packet[ihl:]
    if len(tcp) < 20:
        return None
    src_port, dst_port, seq, ack, offset_res, flags, window = struct.unpack(
        '!HHLLBBH', tcp[:16])
    offset = (offset_res >> 4) * 4
    if offset < 20 or offset > len(tcp):
        return None
    return Segment(socket.inet_ntoa(packet[12:16]), socket.inet_ntoa(packet[16:20]),
                   src_port, dst_port, seq, ack, flags, window, tcp[offset:])


def parse_mss_option(tcp_bytes):
    """Looks for the MSS option in a TCP header.

    Args:
         tcp_bytes  TCP header with its options, starting at the source port

    Returns:
         the MSS value, or None when there is no option or the header is bad
    """
    if len(tcp_bytes) < 20:
        return None
    offset = (tcp_bytes[12] >> 4) * 4
    if offset < 20 or offset > len(tcp_bytes):
        return None
    i = 20
    while i < offset:
        kind = tcp_bytes[i]
        if kind == 0:
            break
        if kind == 1:
            i += 1
            continue
        if i + 1 >= offset:
            break
        length = tcp_bytes[i + 1]
        if length < 2 or i + length > offset:
            break
        if kind == 2 and length == 4:
            return struct.unpack('!H', tcp_bytes[i + 2:i + 4])[0]
        i += length
    return None


def wrap(value):
    """Reduces a sequence number to 32 bits for the wire.

    Args:
         value  unbounded sequence number
    """
    return value % SEQ_MOD


def unwrap(value, near):
    """Turns a 32-bit wire value back into an unbounded sequence number.

    Picks the integer equal to value modulo 2^32 that is closest to near
    (the sequence space of slide 3-79 wraps around).

    Args:
         value  32-bit number read from a header
         near   unbounded number it should be close to
    """
    return near + ((value - near + SEQ_MOD // 2) % SEQ_MOD) - SEQ_MOD // 2


def flag_names(flags):
    """Names the flags that are set, such as 0x12 -> "SYN,ACK".

    Args:
         flags  TCP flag byte
    """
    names = [name for bit, name in ((FIN, 'FIN'), (SYN, 'SYN'), (RST, 'RST'),
                                    (PSH, 'PSH'), (ACK, 'ACK')) if flags & bit]
    return ','.join(names)

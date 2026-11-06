"""Tunable transport parameters. Tests shorten the timeouts to run quickly."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TransportConfig:
    """Tunables of the RUDP layer; times are in seconds, windows in packets."""

    recv_window: int = 1024
    initial_rto: float = 0.5
    min_rto: float = 0.2
    max_rto: float = 3.0
    max_retransmits: int = 15
    dupthresh: int = 3
    ack_delay: float = 0.01
    heartbeat_interval: float = 3.0
    dead_timeout: float = 10.0
    bind_probe_interval: float = 0.25
    tick_interval: float = 0.02
    congestion: str = "cubic"
    initial_cwnd: int = 10
    max_cwnd: int = 2048
    socket_buffer: int = 4 * 1024 * 1024

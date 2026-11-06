"""Congestion controllers working in units of packets: NewReno and CUBIC (RFC 8312, simplified)."""

from __future__ import annotations

import math


class CongestionController:
    """Connection-wide congestion window in packets, shared by all streams."""

    name = "base"

    def __init__(self, initial: int = 10, minimum: int = 2, maximum: int = 2048) -> None:
        self.cwnd = float(initial)
        self.ssthresh = math.inf
        self.minimum = minimum
        self.maximum = maximum

    @property
    def window(self) -> int:
        return max(int(self.cwnd), 1)

    @property
    def in_slow_start(self) -> bool:
        return self.cwnd < self.ssthresh

    def _clamp(self) -> None:
        self.cwnd = min(max(self.cwnd, self.minimum), self.maximum)

    def on_ack(self, acked: int, srtt: float, now: float) -> None:
        raise NotImplementedError

    def on_loss(self, now: float) -> None:
        raise NotImplementedError

    def on_timeout(self, now: float) -> None:
        self.ssthresh = max(self.cwnd * 0.5, self.minimum)
        self.cwnd = self.minimum


class NewReno(CongestionController):
    """Additive increase (one packet per RTT), multiplicative decrease by half."""

    name = "reno"

    def on_ack(self, acked: int, srtt: float, now: float) -> None:
        if self.in_slow_start:
            self.cwnd += acked
        else:
            self.cwnd += acked / self.cwnd
        self._clamp()

    def on_loss(self, now: float) -> None:
        self.cwnd = max(self.cwnd * 0.5, self.minimum)
        self.ssthresh = self.cwnd


class Cubic(CongestionController):
    """Window follows a cubic curve of the time since the last loss, with a Reno floor."""

    name = "cubic"
    C = 0.4
    BETA = 0.7

    def __init__(self, initial: int = 10, minimum: int = 2, maximum: int = 2048) -> None:
        super().__init__(initial, minimum, maximum)
        self.w_max = 0.0
        self.k = 0.0
        self.origin = 0.0
        self.w_est = 0.0
        self.epoch_start: float | None = None

    def on_ack(self, acked: int, srtt: float, now: float) -> None:
        if self.in_slow_start:
            self.cwnd += acked
            self._clamp()
            return
        if self.epoch_start is None:
            self.epoch_start = now
            self.w_est = self.cwnd
            if self.cwnd < self.w_max:
                self.k = ((self.w_max - self.cwnd) / self.C) ** (1 / 3)
                self.origin = self.w_max
            else:
                self.k = 0.0
                self.origin = self.cwnd
        t = now - self.epoch_start + srtt
        target = self.origin + self.C * (t - self.k) ** 3
        # TCP-friendly region: never grow slower than Reno would with the same beta.
        self.w_est += 3 * (1 - self.BETA) / (1 + self.BETA) * acked / self.cwnd
        target = max(target, self.w_est)
        if target > self.cwnd:
            self.cwnd += min((target - self.cwnd) / self.cwnd, 1.0) * acked
        else:
            self.cwnd += 0.01 * acked / self.cwnd
        self._clamp()

    def on_loss(self, now: float) -> None:
        self.w_max = self.cwnd
        self.cwnd = max(self.cwnd * self.BETA, self.minimum)
        self.ssthresh = self.cwnd
        self.epoch_start = None

    def on_timeout(self, now: float) -> None:
        self.w_max = self.cwnd
        self.ssthresh = max(self.cwnd * self.BETA, self.minimum)
        self.cwnd = self.minimum
        self.epoch_start = None


def make_controller(
    name: str, initial: int, maximum: int, minimum: int = 4
) -> CongestionController:
    if name == "reno":
        return NewReno(initial=initial, minimum=minimum, maximum=maximum)
    if name == "cubic":
        return Cubic(initial=initial, minimum=minimum, maximum=maximum)
    raise ValueError(f"unknown congestion controller {name!r}")

import pytest

from streamlink.transport.congestion import Cubic, NewReno, make_controller


@pytest.mark.parametrize("cls", [Cubic, NewReno])
def test_slow_start_then_reduction(cls):
    cc = cls(initial=10)
    for _ in range(20):
        cc.on_ack(1, 0.01, 0.0)
    assert cc.cwnd == 30
    cc.on_loss(1.0)
    assert cc.cwnd < 30
    assert not cc.in_slow_start


@pytest.mark.parametrize("cls", [Cubic, NewReno])
def test_timeout_collapses_window(cls):
    cc = cls(initial=10, minimum=4)
    cc.on_ack(40, 0.01, 0.0)
    cc.on_timeout(1.0)
    assert cc.cwnd == 4
    assert cc.ssthresh >= 4


def test_cubic_recovers_towards_wmax():
    cc = Cubic(initial=100)
    cc.ssthresh = 0  # leave slow start
    cc.on_loss(0.0)
    assert cc.cwnd == pytest.approx(70)
    now = 0.0
    for _ in range(2000):
        now += 0.005
        cc.on_ack(1, 0.02, now)
    assert cc.cwnd > 95  # concave growth back to W_max within a few seconds


def test_window_is_clamped():
    cc = make_controller("reno", initial=10, maximum=12)
    cc.on_ack(100, 0.01, 0.0)
    assert cc.window == 12
    with pytest.raises(ValueError):
        make_controller("bbr", 10, 100)


@pytest.mark.parametrize("name", ["reno", "cubic"])
def test_defaults_follow_slides(name):
    cc = make_controller(name, initial=1, maximum=100)
    assert cc.cwnd == 1            # slide 3-123: initially cwnd = 1 MSS
    cc.on_ack(7, 0.01, 0.0)        # slow start: +1 per ACK
    assert cc.cwnd == 8
    cc.on_timeout(1.0)
    assert cc.cwnd == 1            # slide 3-125: timeout gives cwnd = 1 MSS



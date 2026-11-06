"""Twenty concurrent clients against one server (short version of tools/load_test.py)."""

import argparse
import asyncio

from tools.load_test import run


def _args(**overrides) -> argparse.Namespace:
    values = dict(
        server=None,
        port=0,
        password="loadtest",
        clients=20,
        messages=5,
        interval=0.02,
        file_size=0.25,
        file_pairs=5,
        loss=0.0,
        delay=0.0,
        jitter=0.0,
        cc="cubic",
        timeout=30.0,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_twenty_clients(capsys):
    assert asyncio.run(run(_args())) == 0
    out = capsys.readouterr().out
    assert "2000/2000 deliveries" in out
    assert "5/5 transfers" in out


def test_twenty_clients_lossy_network(capsys):
    assert asyncio.run(run(_args(loss=0.05, delay=5, jitter=3, file_size=0.1))) == 0
    assert "PASS" in capsys.readouterr().out

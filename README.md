# StreamLink

A client-server collaboration app over a **custom reliable protocol on UDP**: login, online list, group and direct chat, file transfer with SHA-256 checks, disconnect detection and reconnect, encryption between each client and the server, and a plain HTML/JS web UI.

All networking uses the Python standard library (`asyncio` sockets). There is no web framework, WebSocket library, or third-party networking stack. The only external library is `cryptography` (X25519, AES-GCM, HKDF).

- Protocol specification: [docs/PROTOCOL.md](docs/PROTOCOL.md)
- Technical report: [docs/REPORT.md](docs/REPORT.md)
- Vietnamese overview: [docs/OVERVIEW.vi.md](docs/OVERVIEW.vi.md)

## Architecture

```
Browser ──HTTP/SSE (127.0.0.1:8080)──> StreamLink client ──TCP 9000: HELLO/AUTH──> Server
                                                             ══UDP 9001: RUDP + AES-GCM══
```

- **Server** (`streamlink/server`): TCP handshake, user authentication, session management, message and file relay.
- **Client** (`streamlink/client`): keeps an RUDP connection to the server and serves a small HTTP server on the user's machine. The browser talks only to this local client: events arrive over Server-Sent Events and commands go out with `fetch`.
- **RUDP** (`streamlink/transport`): per-stream sequence numbers, cumulative ACK + SACK, selective retransmission (fast retransmit + RACK + RTO), sliding window, CUBIC/NewReno congestion control, heartbeat.

## Install

Python 3.9 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

### Server

```bash
python -m streamlink.server
# optional: --host 0.0.0.0 --tcp-port 9000 --udp-port 9001 --users data/users.json
#           --no-register --cc cubic|reno --max-clients 64 --status-interval 10 -v
```

Accounts are stored in `data/users.json` (PBKDF2 password hashes). By default an unknown account is created on first login; pass `--no-register` to turn that off.

If clients run on other machines, open TCP 9000 and UDP 9001 on the server host's firewall.

### Web client (default)

```bash
python -m streamlink.client --server 127.0.0.1 --open
```

Open http://127.0.0.1:8080 and enter a username and password. To run several users on one machine, give each client its own HTTP port and download directory:

```bash
python -m streamlink.client --http-port 8081 --download-dir downloads/nguyen -u nguyen -p 123456
```

With `-u` and `-p`, the client logs in at startup.

### Terminal client

```bash
python -m streamlink.client cli --server 127.0.0.1 -u duy
```

| Command | Meaning |
| --- | --- |
| `text` | send to everyone |
| `/w <name> <text>` | direct message |
| `/send <name> <path>` | send a file |
| `/users`, `/transfers`, `/stats` | online users, transfers, RUDP stats |
| `/quit` | quit |

### Lossy network

`tools/lossy_proxy.py` is a UDP proxy that drops, delays, reorders, and duplicates packets. The client still logs in over TCP, but sends UDP to the proxy:

```bash
python -m tools.lossy_proxy --listen 9101 --target 127.0.0.1:9001 --loss 0.1 --delay 20 --jitter 10
python -m streamlink.client --udp-override 127.0.0.1:9101
```

The web UI "Network" panel shows RTT, RTO, `cwnd`, retransmits, duplicates, and throughput, so the reaction to loss is visible.

## Tests

```bash
pytest -q                 # 46 tests: protocol, RUDP under loss, integration, HTTP gateway, load
ruff check . && ruff format --check .
```

- `tests/test_protocol.py`: header framing, encryption, input checks.
- `tests/test_congestion.py`: CUBIC, NewReno.
- `tests/test_rudp.py`: ordering and fragmentation, 10% and 20% loss, no head-of-line blocking across streams, sliding window, backpressure, garbage packets, timeout.
- `tests/test_integration.py`: messages and presence, bad password, full server, file integrity (0 B to 700 KB), file transfer under loss, peer crash, reconnect after server restart, second login replaces the old session, ciphertext does not leak plaintext.
- `tests/test_web.py`: hand-written HTTP server (static files, API, SSE, upload/download, Origin CSRF check, malformed requests).
- `tests/test_load.py`: 20 concurrent clients, on a clean network and with 5% loss.

### Load test

```bash
python -m tools.load_test --clients 20                                  # embedded server
python -m tools.load_test --clients 20 --loss 0.05 --delay 10 --jitter 5 --file-size 1
python -m tools.load_test --server 192.168.1.10 --port 9000             # existing server
```

Results on a MacBook (loopback, 20 clients, 20 group messages + 20 direct messages each, 5 parallel 2 MB files):

| Scenario | Group messages | Direct messages | Median / p95 latency | Files | Retransmits |
| --- | --- | --- | --- | --- | --- |
| Clean network | 8000/8000 | 800/800 | 21 / 24 ms | 5/5 SHA-256 match, 10 MB/s | 0% |
| 5% loss, 10 ± 5 ms delay | 8000/8000 | 800/800 | 817 / 1737 ms | 5/5 SHA-256 match (1 MB) | 2.7% |

The load test runs all 20 clients and the server in **one** Python process, so latency is mostly CPU time spent fanning out (2 clients: 0.9 ms, 10 clients: 8 ms). Under loss, latency rises because stream 0 delivers chat in order: a lost message holds later messages until it is retransmitted.

## Layout

```
streamlink/
  protocol/   constants.py, packet.py (27-byte header), messages.py (JSON, file chunks, validation)
  crypto/     session.py (X25519, HKDF, AES-256-GCM with the header as AAD)
  transport/  rudp.py (Connection), endpoint.py (UDP socket, demux by session),
              congestion.py (CUBIC, NewReno), config.py
  server/     main.py, handshake.py, users.py, sessions.py, router.py, file_relay.py
  client/     core.py (client logic), cli.py, __main__.py
              web/http_server.py (hand-written HTTP/1.1 + SSE), web/static/ (plain HTML, CSS, JS)
tools/        lossy_proxy.py, load_test.py
tests/        pytest
docs/         PROTOCOL.md, REPORT.md, images
```

"""Terminal user interface on top of ClientCore."""

from __future__ import annotations

import asyncio
import sys
import threading
import time

from streamlink.client.core import ClientCore, NotConnected

HELP = """Commands:
  <message>             send to everyone (broadcast)
  /w <user> <message>   direct message
  /users                who is online
  /send <user> <file>   send a file
  /transfers            file transfers
  /stats                connection stats (RTT, cwnd, retransmits, ...)
  /help                 this help
  /quit                 quit"""


def _clock(ts: float | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts or time.time()))


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _stdin_lines(loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
    """Read stdin in a daemon thread so a blocked read never prevents the program from exiting."""
    queue: asyncio.Queue = asyncio.Queue()

    def reader() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=reader, daemon=True).start()
    return queue


def format_event(event: dict, me: str | None, milestones: dict) -> str | None:
    kind = event["type"]
    if kind == "message":
        ts = _clock(event.get("ts"))
        if event["kind"] == "all":
            return f"[{ts}] <{event['from']}> {event['text']}"
        return f"[{ts}] [dm {event['from']} -> {event['to']}] {event['text']}"
    if kind == "presence":
        user = event["user"]
        if event["event"] == "join":
            return f"*** {user['username']} joined ({user.get('ip')}:{user.get('port')})"
        reason = f" ({event['reason']})" if event.get("reason") else ""
        return f"*** {user['username']} left{reason}"
    if kind == "status":
        reason = f": {event['reason']}" if event.get("reason") else ""
        return f"*** status: {event['state']}{reason}"
    if kind == "error":
        return f"!!! error {event.get('code')}: {event.get('message')}"
    if kind == "file":
        direction = f"-> {event['peer']}" if event["direction"] == "out" else f"<- {event['peer']}"
        key = event["id"]
        step = int(event["percent"] // 25)
        previous = milestones.get(key)
        if previous == (event["state"], step):
            return None
        milestones[key] = (event["state"], step)
        line = (
            f"[file {direction}] {event['name']}: {event['state']} {event['percent']}% "
            f"({_size(event['bytes'])}/{_size(event['size'])}, {_size(event['speed'])}/s)"
        )
        if event.get("error"):
            line += f" - {event['error']}"
        if event["state"] == "done" and event["direction"] == "in":
            line += " - saved, SHA-256 matches"
        return line
    return None


async def _print_events(core: ClientCore, queue: asyncio.Queue) -> None:
    milestones: dict = {}
    while True:
        event = await queue.get()
        line = format_event(event, core.username, milestones)
        if line:
            print(line, flush=True)


def _handle_command(core: ClientCore, line: str) -> bool:
    """Run one input line; returns False when the user wants to quit."""
    if not line.startswith("/"):
        core.send_broadcast(line)
        return True
    command, _, rest = line.partition(" ")
    command = command.lower()
    if command in ("/quit", "/exit", "/q"):
        return False
    if command == "/help":
        print(HELP)
    elif command in ("/w", "/msg", "/dm"):
        to, _, text = rest.strip().partition(" ")
        if not to or not text:
            print("usage: /w <user> <message>")
        else:
            core.send_direct(to, text)
    elif command == "/users":
        core.request_users()
        names = ", ".join(sorted(core.users)) or "(none)"
        print(f"online ({len(core.users)}): {names}")
    elif command == "/send":
        to, _, path = rest.strip().partition(" ")
        if not to or not path:
            print("usage: /send <user> <file path>")
        else:
            tid = core.send_file(path.strip().strip('"'), to)
            print(f"sending (id {tid[:8]})")
    elif command == "/transfers":
        if not core.transfers:
            print("no file transfers yet")
        for t in core.transfers.values():
            info = t.info()
            print(
                f"  {t.id[:8]} {t.direction} {t.peer} {t.name} {info['state']} {info['percent']}%"
            )
    elif command == "/stats":
        if core.conn is None:
            print("not connected")
        else:
            for key, value in core.conn.snapshot().items():
                print(f"  {key:18} {value}")
    else:
        print(f"unknown command {command}, type /help")
    return True


async def run_cli(core: ClientCore, host: str, port: int, username: str, password: str) -> None:
    await core.connect(host, username, password, port)
    queue = core.events.subscribe()
    printer = asyncio.ensure_future(_print_events(core, queue))
    print(f"Logged in as {username}@{host}:{port}. Type /help for commands.")
    lines = _stdin_lines(asyncio.get_running_loop())
    try:
        while True:
            line = await lines.get()
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                if not _handle_command(core, line):
                    break
            except (NotConnected, ValueError, FileNotFoundError, KeyError) as exc:
                print(f"!!! {exc}")
    finally:
        printer.cancel()
        await core.disconnect()
        print("Disconnected.")

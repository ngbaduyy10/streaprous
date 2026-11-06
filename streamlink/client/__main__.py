"""Entry point: ``python -m streamlink.client {web,cli} ...``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import signal
import sys
import webbrowser

from streamlink.client.cli import run_cli
from streamlink.client.core import AuthError, ClientCore
from streamlink.client.web.http_server import WebGateway
from streamlink.protocol.constants import DEFAULT_TCP_PORT
from streamlink.transport.config import TransportConfig


def _addr(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not port.isdigit():
        raise argparse.ArgumentTypeError("expected HOST:PORT")
    return host or "127.0.0.1", int(port)


def _make_core(args: argparse.Namespace) -> ClientCore:
    return ClientCore(
        download_dir=args.download_dir,
        config=TransportConfig(congestion=args.cc),
        udp_override=args.udp_override,
        auto_reconnect=not args.no_reconnect,
    )


async def _run_web(args: argparse.Namespace) -> None:
    core = _make_core(args)
    gateway = WebGateway(
        core,
        host=args.http_host,
        port=args.http_port,
        default_server=(args.server, args.port),
        upload_dir=args.upload_dir,
    )
    await gateway.start()
    url = f"http://{gateway.host}:{gateway.port}/"
    print(f"Web UI: {url}  (Ctrl+C to quit)", flush=True)
    if args.username and args.password:
        try:
            await core.connect(args.server, args.username, args.password, args.port)
        except (AuthError, OSError, asyncio.TimeoutError) as exc:
            print(f"Auto-login failed: {exc}")
    if args.open:
        webbrowser.open(url)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        await core.disconnect()
        await gateway.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m streamlink.client", description="StreamLink client"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", default="127.0.0.1", help="server address (default 127.0.0.1)")
    common.add_argument(
        "--port", type=int, default=DEFAULT_TCP_PORT, help="server TCP port (default 9000)"
    )
    common.add_argument("-u", "--username")
    common.add_argument(
        "-p", "--password", help="omit to be prompted (CLI) or log in from the web page"
    )
    common.add_argument(
        "--download-dir", default="downloads", help="where received files are saved"
    )
    common.add_argument(
        "--udp-override", type=_addr, metavar="HOST:PORT", help="send UDP here (e.g. lossy proxy)"
    )
    common.add_argument(
        "--cc", choices=["cubic", "reno"], default="cubic", help="congestion control"
    )
    common.add_argument(
        "--no-reconnect", action="store_true", help="do not reconnect automatically"
    )
    common.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="mode")

    web = sub.add_parser("web", parents=[common], help="local web UI (default)")
    web.add_argument("--http-host", default="127.0.0.1", help="bind address of the web UI")
    web.add_argument("--http-port", type=int, default=8080, help="port of the web UI (0 = any)")
    web.add_argument("--upload-dir", default="uploads", help="temporary storage for uploads")
    web.add_argument("--open", action="store_true", help="open the browser")

    sub.add_parser("cli", parents=[common], help="terminal UI")

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("web", "cli", "-h", "--help"):
        argv.insert(0, "web")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        if args.mode == "cli":
            username = args.username or input("Username: ").strip()
            password = args.password or getpass.getpass("Password: ")
            asyncio.run(run_cli(_make_core(args), args.server, args.port, username, password))
        else:
            asyncio.run(_run_web(args))
    except AuthError as exc:
        raise SystemExit(f"Login failed: {exc}") from None
    except OSError as exc:
        raise SystemExit(
            f"Could not reach server {args.server}:{args.port}: {exc}"
        ) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

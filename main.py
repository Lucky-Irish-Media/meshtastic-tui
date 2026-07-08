from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import textwrap
import time

from protocol import SOCKET_PATH, send_msg, Message


def _daemon_alive() -> bool:
    if not SOCKET_PATH.exists():
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(str(SOCKET_PATH))
        send_msg(sock, Message("ping"))
        sock.settimeout(2)
        while True:
            buf = b""
            while True:
                c = sock.recv(1)
                if not c:
                    sock.close()
                    return False
                if c == b"\n":
                    break
                buf += c
            if b"pong" in buf:
                sock.close()
                return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


def _spawn_daemon() -> subprocess.Popen:
    subprocess.run(
        ["pkill", "-f", r"python.*-m daemon"],
        capture_output=True,
    )
    SOCKET_PATH.unlink(missing_ok=True)
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Meshtastic TUI - chat with Meshtastic devices",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              %(prog)s                          # normal mode (daemon stops on exit)
              %(prog)s --daemonize              # keep daemon running after TUI exits
              %(prog)s --connect 12:34:56:78:90:AB  # auto-connect to BLE address
        """),
    )
    parser.add_argument(
        "--daemonize",
        action="store_true",
        help="Keep the background daemon running after the TUI exits",
    )
    parser.add_argument(
        "--connect",
        type=str,
        default="",
        help="BLE address to auto-connect on startup",
    )
    args = parser.parse_args()

    daemon_proc = None
    if not _daemon_alive():
        print("Starting background daemon...")
        daemon_proc = _spawn_daemon()
        for _ in range(100):
            if _daemon_alive():
                break
            time.sleep(0.1)
        else:
            print("error: daemon failed to start", file=sys.stderr)
            sys.exit(1)

    from app import main as tui_main
    from app import DaemonClient

    daemon = DaemonClient()

    if args.connect:
        daemon.send("connect", address=args.connect)

    try:
        tui_main(daemon)
    finally:
        daemon.close()

        if daemon_proc and not args.daemonize:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(2)
                sock.connect(str(SOCKET_PATH))
                send_msg(sock, Message("shutdown"))
                sock.close()
            except Exception:
                pass
            try:
                daemon_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon_proc.kill()


if __name__ == "__main__":
    main()

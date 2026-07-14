from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SOCKET_PATH = Path.home() / ".config" / "meshtastic-tui" / "meshtasticd.sock"


@dataclass
class Message:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


def send_msg(sock: socket.socket, msg: Message) -> None:
    data = json.dumps({"type": msg.type, **msg.payload}) + "\n"
    sock.sendall(data.encode())


def recv_msg(sock: socket.socket) -> Message | None:
    buf = b""
    while True:
        c = sock.recv(1)
        if not c:
            return None
        if c == b"\n":
            break
        buf += c
    if not buf:
        return None
    obj = json.loads(buf.decode())
    return Message(type=obj.pop("type"), payload=obj)

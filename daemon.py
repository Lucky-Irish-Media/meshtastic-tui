from __future__ import annotations

import json
import os
import queue
import signal
import socket
import threading
import time
from pathlib import Path

import meshtastic.ble_interface
import meshtastic.protobuf.channel_pb2 as channel_pb2
from pubsub import pub

from protocol import Message, SOCKET_PATH, send_msg

LOG_PATH = SOCKET_PATH.parent / "daemon.log"


def _log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


class Daemon:
    def __init__(self) -> None:
        self.cmd_queue: queue.Queue[Message] = queue.Queue()
        self.event_queue: queue.Queue[Message] = queue.Queue()
        self.sock_path: Path = SOCKET_PATH
        self.server_sock: socket.socket | None = None
        self.client_sock: socket.socket | None = None
        self.reader_thread: threading.Thread | None = None
        self.running = True
        self.interface = None
        self.connected = False
        self.current_address: str | None = None
        self.current_name: str = ""
        self.cached_nodes: list[dict] = []
        self.cached_channels: list[dict] = []

    def start(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.sock_path.exists():
            self.sock_path.unlink()

        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock.bind(str(self.sock_path))
        self.server_sock.listen(1)
        self.server_sock.setblocking(False)

        worker = threading.Thread(target=self._worker_loop, daemon=True)
        worker.start()

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        while self.running:
            try:
                try:
                    conn, _ = self.server_sock.accept()
                    self._on_client_connect(conn)
                except BlockingIOError:
                    pass

                if self.client_sock:
                    self._drain_event_queue()

                time.sleep(0.05)
            except Exception as exc:
                _log(f"Main loop error: {exc}")

        self._shutdown()

    def _handle_signal(self, signum, frame) -> None:  # noqa: ARG002
        self.running = False

    def _on_client_connect(self, conn: socket.socket) -> None:
        self._close_client()
        self.client_sock = conn
        self.reader_thread = threading.Thread(
            target=self._client_reader, daemon=True
        )
        self.reader_thread.start()
        self._send_state()

    def _client_reader(self) -> None:
        while self.running and self.client_sock:
            try:
                msg = self._recv_msg(self.client_sock)
                if msg is None:
                    break
                self.cmd_queue.put(msg)
            except (ConnectionError, OSError):
                break
        self._close_client()

    @staticmethod
    def _recv_msg(sock: socket.socket) -> Message | None:
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

    def _send_state(self) -> None:
        if not self.client_sock:
            return
        if self.connected:
            try:
                send_msg(
                    self.client_sock,
                    Message(
                        "connection_established",
                        {
                            "address": self.current_address or "",
                            "name": self.current_name,
                        },
                    ),
                )
                send_msg(
                    self.client_sock,
                    Message("nodes", {"nodes": self.cached_nodes}),
                )
                send_msg(
                    self.client_sock,
                    Message("channels", {"channels": self.cached_channels}),
                )
            except Exception as exc:
                _log(f"_send_state error: {exc}")
                self._close_client()

    def _drain_event_queue(self) -> None:
        if not self.client_sock:
            return
        while self.running:
            try:
                msg = self.event_queue.get_nowait()
                try:
                    send_msg(self.client_sock, msg)
                except (BrokenPipeError, ConnectionError):
                    self._close_client()
                    return
            except queue.Empty:
                break

    def _close_client(self) -> None:
        if self.client_sock:
            try:
                self.client_sock.close()
            except Exception:
                pass
            self.client_sock = None

    def _shutdown(self) -> None:
        self._close_interface()
        self._close_client()
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
        if self.sock_path.exists():
            self.sock_path.unlink()

    def _close_interface(self) -> None:
        if self.interface:
            try:
                self.interface.close()
            except Exception:
                pass
            self.interface = None
        self.connected = False
        self.current_address = None
        self.current_name = ""
        self._unsubscribe_all()

    def _worker_loop(self) -> None:
        while self.running:
            try:
                msg = self.cmd_queue.get(timeout=0.5)
                self._handle_command(msg)
            except queue.Empty:
                continue
            except Exception as exc:
                self.event_queue.put(
                    Message("error", {"message": f"Worker error: {exc}"})
                )

    def _handle_command(self, msg: Message) -> None:
        handler = {
            "scan": self._cmd_scan,
            "connect": self._cmd_connect,
            "disconnect": self._cmd_disconnect,
            "send_text": self._cmd_send_text,
            "get_nodes": self._cmd_get_nodes,
            "get_channels": self._cmd_get_channels,
            "ping": self._cmd_ping,
            "shutdown": self._cmd_shutdown,
        }.get(msg.type)
        if handler:
            handler(msg)
        else:
            self.event_queue.put(
                Message("error", {"message": f"Unknown command: {msg.type}"})
            )

    def _cmd_scan(self, msg: Message) -> None:  # noqa: ARG002
        def scan_worker() -> None:
            try:
                devices = meshtastic.ble_interface.BLEInterface.scan()
            except Exception as exc:
                self.event_queue.put(
                    Message("scan_result", {"devices": [], "error": str(exc)})
                )
                return
            results = [
                {"name": d.name or "Unknown", "address": d.address}
                for d in devices
            ]
            self.event_queue.put(
                Message("scan_result", {"devices": results})
            )

        threading.Thread(target=scan_worker, daemon=True).start()

    def _cmd_connect(self, msg: Message) -> None:
        address = msg.payload.get("address", "")
        if not address:
            self.event_queue.put(
                Message("connection_failed", {"error": "No address provided"})
            )
            return

        if self.connected and self.current_address == address:
            self._send_state()
            return

        if self.connected:
            self._close_interface()

        self.current_address = address

        def connect_worker() -> None:
            pub.subscribe(
                self._on_connection_established,
                "meshtastic.connection.established",
            )
            pub.subscribe(
                self._on_node_updated,
                "meshtastic.node.updated",
            )
            pub.subscribe(
                self._on_text_msg,
                "meshtastic.receive.text",
            )
            pub.subscribe(
                self._on_disconnected,
                "meshtastic.connection.lost",
            )

            try:
                interface = meshtastic.ble_interface.BLEInterface(
                    address=address
                )
            except Exception as exc:
                self._unsubscribe_all()
                self.event_queue.put(
                    Message(
                        "connection_failed", {"error": f"Connection failed: {exc}"}
                    )
                )
                return

            self.interface = interface
            self.connected = True
            self.current_name = getattr(interface, "name", "") or ""
            self._cache_channels()
            self._cache_nodes()
            self.event_queue.put(
                Message("channels", {"channels": self.cached_channels})
            )
            self.event_queue.put(
                Message("nodes", {"nodes": self.cached_nodes})
            )
            self.event_queue.put(
                Message(
                    "connection_established", {"address": address}
                )
            )

        threading.Thread(target=connect_worker, daemon=True).start()

    def _unsubscribe_all(self) -> None:
        subs = [
            ("meshtastic.connection.established", self._on_connection_established),
            ("meshtastic.node.updated", self._on_node_updated),
            ("meshtastic.receive.text", self._on_text_msg),
            ("meshtastic.connection.lost", self._on_disconnected),
        ]
        for topic, handler in subs:
            try:
                pub.unsubscribe(handler, topic)  # type: ignore[arg-type]
            except Exception:
                pass

    def _on_connection_established(self, interface=None) -> None:  # noqa: ARG002
        iface = interface or self.interface
        if iface:
            self._cache_channels(iface)
            self._cache_nodes(iface)
            self.event_queue.put(
                Message("channels", {"channels": self.cached_channels})
            )
            self.event_queue.put(
                Message("nodes", {"nodes": self.cached_nodes})
            )

    def _on_text_msg(self, packet, interface) -> None:  # noqa: ARG002
        self.event_queue.put(
            Message("text_received", {"packet": packet})
        )

    def _on_disconnected(self, interface, **kwargs) -> None:  # noqa: ARG002
        self.connected = False
        self._close_interface()
        self.event_queue.put(
            Message("connection_lost", {"reason": "Device disconnected"})
        )

    def _on_node_updated(self, node, interface) -> None:  # noqa: ARG002
        self._cache_nodes(interface)
        self.event_queue.put(Message("nodes", {"nodes": self.cached_nodes}))

    def _cache_channels(self, iface=None) -> None:
        iface = iface or self.interface
        if not iface or not iface.localNode or not iface.localNode.channels:
            return
        channels = []
        try:
            for ch in iface.localNode.channels:
                if ch.role == channel_pb2.Channel.DISABLED:
                    continue
                role = "P" if ch.role == channel_pb2.Channel.PRIMARY else "S"
                channels.append(
                    {
                        "index": ch.index,
                        "name": ch.settings.name or f"Channel {ch.index}",
                        "role": role,
                    }
                )
        except Exception:
            pass
        self.cached_channels = channels

    def _cache_nodes(self, iface=None) -> None:
        iface = iface or self.interface
        if not iface or not iface.nodes:
            return
        nodes = []
        try:
            for node in iface.nodes.values():
                user = node.get("user", {})
                nodes.append(
                    {
                        "id": user.get("id", "?"),
                        "shortName": user.get("shortName", "?"),
                        "longName": user.get("longName", "?"),
                    }
                )
            nodes.sort(key=lambda n: (n["longName"] or ""))
        except Exception:
            pass
        self.cached_nodes = nodes

    def _cmd_disconnect(self, msg: Message) -> None:  # noqa: ARG002
        self._close_interface()
        self.event_queue.put(
            Message("connection_lost", {"reason": "Disconnected by user"})
        )

    def _cmd_send_text(self, msg: Message) -> None:
        if not self.interface or not self.connected:
            self.event_queue.put(
                Message("error", {"message": "Not connected"})
            )
            return
        text = msg.payload.get("text", "")
        dest = msg.payload.get("destinationId", "^all")
        ch = msg.payload.get("channelIndex", 0)
        try:
            self.interface.sendText(
                text,
                destinationId=dest,
                channelIndex=ch,
            )
        except Exception as exc:
            self.event_queue.put(
                Message("error", {"message": f"Send failed: {exc}"})
            )

    def _cmd_get_nodes(self, msg: Message) -> None:  # noqa: ARG002
        self.event_queue.put(Message("nodes", {"nodes": self.cached_nodes}))

    def _cmd_get_channels(self, msg: Message) -> None:  # noqa: ARG002
        self.event_queue.put(Message("channels", {"channels": self.cached_channels}))

    def _cmd_ping(self, msg: Message) -> None:  # noqa: ARG002
        self.event_queue.put(Message("pong"))

    def _cmd_shutdown(self, msg: Message) -> None:  # noqa: ARG002
        self.running = False


def main() -> None:
    daemon = Daemon()
    daemon.start()


if __name__ == "__main__":
    main()

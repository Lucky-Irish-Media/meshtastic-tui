from __future__ import annotations

import json
import queue
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    TabbedContent,
    TabPane,
)

from protocol import SOCKET_PATH, Message, recv_msg, send_msg


class DaemonDisconnected(Exception):
    pass


class DaemonClient:
    def __init__(self) -> None:
        self.sock: socket.socket | None = None
        self.running = True
        self.reader_thread: threading.Thread | None = None
        self.inbox: queue.Queue = queue.Queue()
        self.connected = False
        self._connect()

    def _connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(SOCKET_PATH))
        self.connected = True
        self.reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True
        )
        self.reader_thread.start()

    def _reader_loop(self) -> None:
        while self.running and self.sock:
            try:
                msg = recv_msg(self.sock)
                if msg is None:
                    break
                self.inbox.put(msg)
            except (ConnectionError, OSError):
                break
        self.connected = False
        self.inbox.put(DaemonDisconnected)

    def send(self, msg_type: str, **payload: Any) -> None:
        if not self.sock or not self.connected:
            return
        try:
            send_msg(self.sock, Message(msg_type, payload))
        except (ConnectionError, OSError):
            self.connected = False
            self.inbox.put(DaemonDisconnected)

    def poll(self) -> Any:
        try:
            return self.inbox.get_nowait()
        except queue.Empty:
            return None

    def reconnect(self) -> bool:
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self.connected = False

        for attempt in range(50):
            try:
                self._connect()
                return True
            except (ConnectionRefusedError, FileNotFoundError):
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                time.sleep(0.1)
        return False

    def close(self) -> None:
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


FAVORITES_FILE = Path.home() / ".config" / "meshtastic-tui" / "favorites.json"


class DeviceScreen(Screen):
    BINDINGS: ClassVar = [Binding("q", "app.quit", "Quit", priority=True)]

    @property
    def daemon(self) -> DaemonClient:
        return self.app.daemon  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Label("Scanning for Meshtastic BLE devices...", id="scan-status"),
            ListView(id="device-list"),
            Button("Scan Again", id="scan-btn", variant="primary"),
            id="device-screen",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._poll_handle = self.set_interval(0.05, self._poll_daemon)
        self._start_scan()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "scan-btn":
            self._start_scan()

    def _start_scan(self) -> None:
        btn = self.query_one("#scan-btn", Button)
        status = self.query_one("#scan-status", Label)
        device_list = self.query_one("#device-list", ListView)

        btn.disabled = True
        status.update("Scanning for 10 seconds...")
        device_list.clear()
        self.daemon.send("scan")

    def _stop_polling(self) -> None:
        if self._poll_handle:
            self._poll_handle.stop()
            self._poll_handle = None

    def _poll_daemon(self) -> None:
        if self.app.screen is not self:
            return
        while True:
            msg = self.daemon.poll()
            if msg is DaemonDisconnected:
                self._daemon_disconnected()
                return
            if msg is None:
                return
            self._handle_msg(msg)

    def _handle_msg(self, msg: Message) -> None:
        if msg.type == "scan_result":
            self._scan_complete(msg.payload.get("devices", []))
        elif msg.type == "error":
            self._scan_failed(msg.payload.get("message", "Unknown error"))
        elif msg.type == "connection_established":
            self._stop_polling()
            address = msg.payload.get("address", "")
            name = msg.payload.get("name", address)
            self.app.push_screen(ChatScreen(address, name))

    def _scan_failed(self, error: str) -> None:
        try:
            status = self.query_one("#scan-status", Label)
            btn = self.query_one("#scan-btn", Button)
            status.update(f"[red]Scan failed: {error}[/]")
            btn.disabled = False
        except Exception:
            pass

    def _scan_complete(self, devices: list[dict]) -> None:
        try:
            status = self.query_one("#scan-status", Label)
            btn = self.query_one("#scan-btn", Button)
            device_list = self.query_one("#device-list", ListView)
        except Exception:
            return

        if not devices:
            try:
                status.update("No Meshtastic devices found.")
                btn.disabled = False
            except Exception:
                pass
            return

        try:
            status.update(
                f"Found {len(devices)} device(s). Select one to connect."
            )
            for d in devices:
                item = ListItem(Label(f"{d['name']}  [{d['address']}]"))
                item.data = d
                device_list.append(item)
            btn.disabled = False
        except Exception:
            pass

    def on_screen_resume(self) -> None:
        self._poll_handle = self.set_interval(0.05, self._poll_daemon)
        try:
            device_list = self.query_one("#device-list", ListView)
            status = self.query_one("#scan-status", Label)
            btn = self.query_one("#scan-btn", Button)
            btn.disabled = False
            if device_list.child_count > 0:
                status.update("Select a device to connect, or scan again.")
            else:
                status.update("No devices found. Press Scan Again.")
        except Exception:
            pass

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        device = event.item.data
        if device is None:
            return
        address = device.get("address", "")
        if not address:
            return
        self.app.push_screen(ChatScreen(address, device.get("name", "Unknown")))

    def _daemon_disconnected(self) -> None:
        try:
            status = self.query_one("#scan-status", Label)
            btn = self.query_one("#scan-btn", Button)
            status.update("[red]Lost connection to daemon. Retrying...[/]")
            btn.disabled = True
        except Exception:
            pass
        if self.daemon.reconnect():
            self._start_scan()


class ChatScreen(Screen):
    BINDINGS: ClassVar = [
        Binding("q", "app.quit", "Quit", priority=True),
        Binding("escape", "disconnect", "Disconnect"),
        Binding("c", "focus_channels", "Channels", priority=True),
        Binding("n", "focus_nodes", "Nodes", priority=True),
        Binding("b", "broadcast", "Broadcast", priority=True),
        Binding("f", "toggle_favorite", "Fav", priority=True),
        Binding("ctrl+w", "close_tab", "Close Tab", priority=True),
    ]

    @property
    def daemon(self) -> DaemonClient:
        return self.app.daemon  # type: ignore[attr-defined]

    def __init__(self, address: str, name: str = "") -> None:
        self._address = address
        self._device_name = name
        self._connected = False
        self._cleaned_up = False
        self._connection_timer: Any = None
        self._poll_handle = None
        self._channel_index = 0
        self._destination_id: str | None = None
        self._favorites: set[str] = set()
        self._mesh_nodes: dict[str, dict] = {}
        self._channels: dict[int, dict] = {}
        self._tab_targets: dict[str, tuple[str, Any]] = {
            "tab-broadcast": ("broadcast", None),
        }
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Horizontal(
            TabbedContent(id="chat-tabs"),
            VerticalScroll(
                Label("Channels", classes="sidebar-header"),
                ListView(id="channel-list"),
                Label("Nodes", classes="sidebar-header", id="nodes-header"),
                ListView(id="node-list"),
                id="sidebar",
            ),
            id="chat-area",
        )
        yield Container(
            Input(
                placeholder="Type a message and press Enter to send...",
                id="msg-input",
            ),
            id="input-container",
        )
        yield Footer()

    @staticmethod
    def _sanitize_id(raw: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", raw)

    def _write_to_tab(self, tab_id: str, msg: str) -> None:
        self.query_one(f"#log-{tab_id}", RichLog).write(msg)

    def _get_or_create_tab(
        self, tab_id: str, title: str, kind: str, value: Any
    ) -> RichLog:
        try:
            return self.query_one(f"#log-{tab_id}", RichLog)
        except Exception:
            tabs = self.query_one("#chat-tabs", TabbedContent)
            log = RichLog(
                id=f"log-{tab_id}", highlight=True, markup=True, wrap=True
            )
            pane = TabPane(title, log, id=tab_id)
            tabs.add_pane(pane)
            self._tab_targets[tab_id] = (kind, value)
            return log

    def _get_node_name(self, node_id: str) -> str:
        node = self._mesh_nodes.get(node_id)
        if node:
            short = node.get("shortName", "")
            long_name = node.get("longName", node_id)
            return f"{short} {long_name}" if short else long_name
        return node_id

    def _get_channel_name(self, ch_index: int) -> str:
        ch = self._channels.get(ch_index)
        if ch:
            return ch.get("name", f"Channel {ch_index}")
        return f"Channel {ch_index}"

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        tab_id = event.pane.id or "tab-broadcast"
        kind, value = self._tab_targets.get(tab_id, ("broadcast", None))
        if kind == "broadcast":
            self._channel_index = 0
            self._destination_id = None
        elif kind == "channel":
            self._channel_index = value
            self._destination_id = None
        elif kind == "node":
            self._destination_id = value
            self._channel_index = 0

    def on_mount(self) -> None:
        tabs = self.query_one("#chat-tabs", TabbedContent)
        broadcast_pane = TabPane(
            "Broadcast",
            RichLog(
                id="log-tab-broadcast", highlight=True, markup=True, wrap=True
            ),
            id="tab-broadcast",
        )
        tabs.add_pane(broadcast_pane)
        self._tab_targets["tab-broadcast"] = ("broadcast", None)
        self._write_to_tab("tab-broadcast", "[yellow]Connecting...[/]")
        self._poll_handle = self.set_interval(0.05, self._poll_daemon)
        self.daemon.send("connect", address=self._address)

    def _poll_daemon(self) -> None:
        if self.app.screen is not self:
            return
        while True:
            msg = self.daemon.poll()
            if msg is DaemonDisconnected:
                self._daemon_disconnected()
                return
            if msg is None:
                return
            self._handle_msg(msg)

    def _handle_msg(self, msg: Message) -> None:
        try:
            handler = {
                "connection_established": self._on_connected,
                "connection_failed": self._on_connection_failed,
                "connection_lost": self._handle_disconnect,
                "text_received": self._display_packet,
                "nodes": self._on_nodes,
                "channels": self._on_channels,
                "error": self._on_error,
            }.get(msg.type)
            if handler:
                handler(msg)
        except Exception:
            pass

    def _on_connected(self, msg: Message) -> None:  # noqa: ARG002
        if self._cleaned_up:
            return
        self._connected = True
        self._write_to_tab("tab-broadcast", "[bold green]Connected![/]")
        self._load_favorites()
        self._populate_channels()
        self._populate_nodes()
        self._update_subtitle()

    def _on_connection_failed(self, msg: Message) -> None:
        if self._cleaned_up:
            return
        error = msg.payload.get("error", "Unknown error")
        self._write_to_tab(
            "tab-broadcast", f"[bold red]Connection failed: {error}[/]"
        )
        self.sub_title = "Disconnected"
        self._connection_timer = self.set_timer(1.5, self.app.pop_screen)

    def _on_nodes(self, msg: Message) -> None:
        self._mesh_nodes = {
            n["id"]: n for n in msg.payload.get("nodes", [])
        }
        self._populate_nodes()

    def _on_channels(self, msg: Message) -> None:
        self._channels = {
            ch["index"]: ch for ch in msg.payload.get("channels", [])
        }
        self._populate_channels()

    def _on_error(self, msg: Message) -> None:
        self._write_to_tab(
            "tab-broadcast",
            f"[bold red]Error: {msg.payload.get('message', '')}[/]",
        )

    def _update_subtitle(self) -> None:
        name = self._device_name or self._address
        if self._destination_id:
            self.sub_title = f"{name}  DM to {self._destination_id}"
        else:
            ch = self._channel_index
            self.sub_title = f"{name}  Channel {ch}"

    def _load_favorites(self) -> None:
        try:
            if FAVORITES_FILE.exists():
                data = json.loads(FAVORITES_FILE.read_text())
                self._favorites = set(data.get("favorites", []))
        except Exception:
            self._favorites = set()

    def _save_favorites(self) -> None:
        try:
            FAVORITES_FILE.parent.mkdir(parents=True, exist_ok=True)
            FAVORITES_FILE.write_text(
                json.dumps({"favorites": sorted(self._favorites)}, indent=2)
            )
        except Exception:
            pass

    def _populate_channels(self) -> None:
        try:
            ch_list = self.query_one("#channel-list", ListView)
        except Exception:
            return
        ch_list.clear()

        try:
            for ch_idx in sorted(self._channels):
                ch = self._channels[ch_idx]
                role = ch.get("role", "S")
                name = ch.get("name", f"CH{ch_idx}")
                label = Label(f"[{role}] {name}  (ch {ch_idx})")
                item = ListItem(label)
                item.data = ("channel", ch_idx)
                ch_list.append(item)
        except Exception:
            pass

    def _populate_nodes(self) -> None:
        try:
            node_list = self.query_one("#node-list", ListView)
        except Exception:
            return
        node_list.clear()

        try:
            known = sorted(
                self._mesh_nodes.values(),
                key=lambda n: (
                    n.get("id", "") not in self._favorites,
                    n.get("longName", ""),
                ),
            )
        except Exception:
            return

        try:
            for node in known:
                nid = node.get("id", "?")
                short = node.get("shortName", "?")
                long_name = node.get("longName", "?")
                star = "★" if nid in self._favorites else " "
                label = Label(f"{star} {short:<5} {long_name}")
                item = ListItem(label)
                item.data = ("node", nid, long_name, short)
                node_list.append(item)
        except Exception:
            return

        try:
            self.query_one("#nodes-header", Label).update(
                f"Nodes ({len(known)})"
            )
        except Exception:
            pass

    def _handle_disconnect(self, msg: Message) -> None:
        if self._cleaned_up:
            return
        self._connected = False
        reason = msg.payload.get("reason", "Disconnected")
        self._write_to_tab("tab-broadcast", f"[bold yellow]{reason}[/]")
        self.sub_title = "Disconnected"

    def _display_packet(self, msg: Message) -> None:
        if not self._connected:
            return

        packet = msg.payload.get("packet", {})
        from_id = packet.get("fromId", "?")
        text = packet.get("decoded", {}).get("text", "")
        rx_snr = packet.get("rxSnr")
        ts = packet.get("rxTime", 0)
        ch = packet.get("channel", 0)
        to_id = packet.get("toId", "")

        tm = (
            time.strftime("%H:%M:%S", time.localtime(ts))
            if ts
            else time.strftime("%H:%M:%S")
        )

        entry = Text.assemble()
        entry.append(f"{tm} ", "dim")

        is_dm = bool(to_id and to_id != "^all")
        if is_dm:
            tab_id = self._sanitize_id(f"tab-{from_id}")
            node_name = self._get_node_name(from_id)
            entry.append("[DM] ", "bold magenta")
        else:
            tab_id = self._sanitize_id(f"tab-ch-{ch}")
            node_name = ""
            if ch != 0:
                entry.append(f"[ch{ch}] ", "bold yellow")

        title = node_name if is_dm else self._get_channel_name(ch)
        kind = "node" if is_dm else "channel"
        value = from_id if is_dm else ch

        log = self._get_or_create_tab(tab_id, title, kind, value)

        entry.append(f"[{from_id}] ", "bold cyan")
        entry.append(text)

        if rx_snr is not None:
            entry.append(f"  ({rx_snr:.1f} dB)", "dim")

        log.write(entry)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not event.item or not event.item.data:
            return
        kind = event.item.data[0]
        value = event.item.data[1]

        if kind == "channel":
            tab_id = self._sanitize_id(f"tab-ch-{value}")
            title = self._get_channel_name(value)
        elif kind == "node":
            tab_id = self._sanitize_id(f"tab-{value}")
            long_name = (
                event.item.data[2] if len(event.item.data) > 2 else value
            )
            short_name = (
                event.item.data[3] if len(event.item.data) > 3 else ""
            )
            title = f"{short_name} {long_name}" if short_name else long_name
        else:
            return

        self._get_or_create_tab(tab_id, title, kind, value)

        tabs = self.query_one("#chat-tabs", TabbedContent)
        if tab_id != tabs.active:
            tabs.active = tab_id

        try:
            self.query_one("#msg-input", Input).focus()
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or not self._connected:
            return

        self.daemon.send(
            "send_text",
            text=text,
            destinationId=self._destination_id or "^all",
            channelIndex=self._channel_index,
        )

        tabs = self.query_one("#chat-tabs", TabbedContent)
        active_id = tabs.active
        tm = time.strftime("%H:%M:%S")
        entry = Text.assemble()
        entry.append(f"{tm} ", "dim")
        if self._destination_id:
            entry.append(
                f"[Me -> {self._destination_id}] ", "bold green"
            )
        else:
            entry.append(
                f"[Me (ch{self._channel_index})] ", "bold green"
            )
        entry.append(text)
        if active_id:
            try:
                self.query_one(f"#log-{active_id}", RichLog).write(entry)
            except Exception:
                pass
        try:
            self.query_one("#msg-input", Input).value = ""
        except Exception:
            pass

    def action_focus_channels(self) -> None:
        try:
            self.query_one("#channel-list", ListView).focus()
        except Exception:
            pass

    def action_focus_nodes(self) -> None:
        try:
            self.query_one("#node-list", ListView).focus()
        except Exception:
            pass

    def action_broadcast(self) -> None:
        tabs = self.query_one("#chat-tabs", TabbedContent)
        if "tab-broadcast" in self._tab_targets:
            tabs.active = "tab-broadcast"
        try:
            self.query_one("#msg-input", Input).focus()
        except Exception:
            pass

    def action_close_tab(self) -> None:
        tabs = self.query_one("#chat-tabs", TabbedContent)
        active_id = tabs.active
        if not active_id or active_id == "tab-broadcast":
            return
        tabs.remove_pane(active_id)
        self._tab_targets.pop(active_id, None)

    def action_toggle_favorite(self) -> None:
        try:
            node_list = self.query_one("#node-list", ListView)
        except Exception:
            return
        if node_list.index is None or not node_list.children:
            return
        item = node_list.children[node_list.index]
        if not item or not item.data or item.data[0] != "node":
            return
        nid = item.data[1]
        if nid in self._favorites:
            self._favorites.discard(nid)
        else:
            self._favorites.add(nid)
        self._save_favorites()
        self._populate_nodes()

    def action_disconnect(self) -> None:
        self._cleanup()
        self.app.pop_screen()

    def _daemon_disconnected(self) -> None:
        self._write_to_tab(
            "tab-broadcast",
            "[bold yellow]Lost connection to daemon. Retrying...[/]",
        )
        if self.daemon.reconnect():
            self._write_to_tab(
                "tab-broadcast",
                "[bold green]Reconnected to daemon.[/]",
            )
            self.daemon.send("connect", address=self._address)

    def _cleanup(self, send_disconnect: bool = True) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        if self._connection_timer:
            self._connection_timer.stop()
            self._connection_timer = None
        if send_disconnect:
            self.daemon.send("disconnect")
        self._connected = False

    def on_unmount(self) -> None:
        if self._poll_handle:
            self._poll_handle.stop()
            self._poll_handle = None
        self._cleanup(send_disconnect=False)


class MeshtasticTUI(App):
    TITLE = "Meshtastic TUI"
    SUB_TITLE = "BLE Chat"
    SCREENS: ClassVar = {"device": DeviceScreen}

    def __init__(self, daemon: DaemonClient) -> None:
        self.daemon = daemon
        super().__init__()

    @property
    def daemon(self) -> DaemonClient:
        return self._daemon

    @daemon.setter
    def daemon(self, value: DaemonClient) -> None:
        self._daemon = value

    CSS = """
    #device-screen {
        align: center middle;
        height: 100%;
    }
    #device-screen #scan-status {
        margin-bottom: 1;
        text-align: center;
    }
    #device-screen ListView {
        width: 60;
        height: 12;
        margin-bottom: 1;
        border: solid $primary;
    }
    #device-screen #scan-btn {
        width: 30;
    }
    #chat-area {
        height: 1fr;
    }
    #chat-tabs {
        width: 1fr;
        height: 100%;
        border: solid $primary;
    }
    #sidebar {
        width: 30;
        height: 100%;
        border: solid $primary;
        margin-left: 1;
        overflow-y: auto;
    }
    #sidebar .sidebar-header {
        text-style: bold;
        padding: 0 1;
        background: $primary-background;
        color: $primary;
        height: 1;
    }
    #channel-list {
        height: auto;
        max-height: 12;
        border: none;
        margin: 0 0 1 0;
    }
    #node-list {
        height: 1fr;
        border: none;
        margin: 0 0 1 0;
    }
    #sidebar ListView:focus {
        border: none;
    }
    #input-container {
        height: 3;
        margin: 0 1 1 1;
    }
    """

    def on_mount(self) -> None:
        self.push_screen("device")


def main(daemon: DaemonClient) -> None:
    app = MeshtasticTUI(daemon)
    app.run()

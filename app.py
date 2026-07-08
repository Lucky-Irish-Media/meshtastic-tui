from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

import meshtastic.ble_interface
import meshtastic.protobuf.channel_pb2 as channel_pb2
from pubsub import pub
from rich.text import Text
from textual import work
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


class DeviceScreen(Screen):
    BINDINGS: ClassVar = [Binding("q", "app.quit", "Quit", priority=True)]

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
        self._do_scan()

    @work(thread=True)
    def _do_scan(self) -> None:
        try:
            devices = meshtastic.ble_interface.BLEInterface.scan()
        except Exception as exc:
            self.app.call_from_thread(self._scan_failed, str(exc))
            return

        self.app.call_from_thread(self._scan_complete, devices)

    def _scan_failed(self, error: str) -> None:
        try:
            status = self.query_one("#scan-status", Label)
            btn = self.query_one("#scan-btn", Button)
            status.update(f"[red]Scan failed: {error}[/]")
            btn.disabled = False
        except Exception:
            pass

    def _scan_complete(self, devices) -> None:
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
            status.update(f"Found {len(devices)} device(s). Select one to connect.")
            for d in devices:
                item = ListItem(Label(f"{d.name or 'Unknown'}  [{d.address}]"))
                item.data = d
                device_list.append(item)
            btn.disabled = False
        except Exception:
            pass

    def on_screen_resume(self) -> None:
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
        self.app.push_screen(ChatScreen(device))


FAVORITES_FILE = Path.home() / ".config" / "meshtastic-tui" / "favorites.json"


class ChatScreen(Screen):
    BINDINGS: ClassVar = [
        Binding("q", "app.quit", "Quit", priority=True),
        Binding("escape", "disconnect", "Disconnect"),
        Binding("c", "focus_channels", "Channels", priority=True),
        Binding("n", "focus_nodes", "Nodes", priority=True),
        Binding("b", "broadcast", "Broadcast", priority=True),
        Binding("f", "toggle_favorite", "Fav", priority=True),
    ]

    def __init__(self, device) -> None:
        self._device = device
        self._interface = None
        self._connected = False
        self._cleaned_up = False
        self._connection_timer = None
        self._connect_timer = None
        self._connecting = False
        self._channel_index = 0
        self._destination_id = None
        self._favorites: set[str] = set()
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
            Input(placeholder="Type a message and press Enter to send...", id="msg-input"),
            id="input-container",
        )
        yield Footer()

    @staticmethod
    def _sanitize_id(raw: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_-]', '_', raw)

    def _write_to_tab(self, tab_id: str, msg: str) -> None:
        self.query_one(f"#log-{tab_id}", RichLog).write(msg)

    def _get_or_create_tab(self, tab_id: str, title: str, kind: str, value: Any) -> RichLog:
        try:
            return self.query_one(f"#log-{tab_id}", RichLog)
        except Exception:
            tabs = self.query_one("#chat-tabs", TabbedContent)
            log = RichLog(id=f"log-{tab_id}", highlight=True, markup=True, wrap=True)
            pane = TabPane(title, log, id=tab_id)
            tabs.add_pane(pane)
            self._tab_targets[tab_id] = (kind, value)
            return log

    def _get_node_name(self, node_id: str) -> str:
        if self._interface and self._interface.nodes:
            for n in self._interface.nodes.values():
                user = n.get("user", {})
                if user.get("id") == node_id:
                    short = user.get("shortName", "")
                    long_name = user.get("longName", node_id)
                    return f"{short} {long_name}" if short else long_name
        return node_id

    def _get_channel_name(self, ch_index: int) -> str:
        if self._interface and self._interface.localNode and self._interface.localNode.channels:
            for ch in self._interface.localNode.channels:
                if ch.index == ch_index:
                    return ch.settings.name or f"Channel {ch_index}"
        return f"Channel {ch_index}"

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab_id = event.pane.id
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
            RichLog(id="log-tab-broadcast", highlight=True, markup=True, wrap=True),
            id="tab-broadcast",
        )
        tabs.add_pane(broadcast_pane)
        self._tab_targets["tab-broadcast"] = ("broadcast", None)
        self._write_to_tab("tab-broadcast", "[yellow]Connecting...[/]")
        self._do_connect()

    @work(thread=True)
    def _do_connect(self) -> None:
        self._connecting = True
        self.app.call_from_thread(self._start_connect_timer)

        pub.subscribe(self._on_connection_established, "meshtastic.connection.established")

        try:
            interface = meshtastic.ble_interface.BLEInterface(
                address=self._device.address
            )
        except Exception as exc:
            try:
                pub.unsubscribe(
                    self._on_connection_established, "meshtastic.connection.established"
                )
            except Exception:
                pass
            self.app.call_from_thread(self._on_connection_failed, str(exc))
            return

        if self._cleaned_up or not self._connecting:
            self._close_interface(interface)
            return

        self.app.call_from_thread(self._on_connected, interface)

    @staticmethod
    def _close_interface(interface) -> None:
        try:
            interface.close()
        except Exception:
            pass

    def _start_connect_timer(self) -> None:
        self._connect_timer = self.set_timer(30, self._on_connect_timed_out)

    def _on_connect_timed_out(self) -> None:
        if self._cleaned_up or not self._connecting:
            return
        self._on_connection_failed("Connection timed out")

    def _on_connected(self, interface) -> None:
        if self._cleaned_up or not self._connecting:
            self._cancel_connect_timer()
            self._close_interface(interface)
            return
        self._cancel_connect_timer()
        self._connecting = False
        self._interface = interface
        self._connected = True
        self._write_to_tab("tab-broadcast", "[bold green]Connected![/]")

        pub.subscribe(self._on_text_msg, "meshtastic.receive.text")
        pub.subscribe(self._on_disconnected, "meshtastic.connection.lost")
        pub.subscribe(self._on_node_updated, "meshtastic.node.updated")

        self._load_favorites()
        self._populate_channels()
        self._populate_nodes()

    def _on_connection_established(self, interface=None) -> None:
        self._call_on_main(self._populate_channels)
        self._call_on_main(self._populate_nodes)

    def _on_connection_failed(self, error: str) -> None:
        if self._cleaned_up or not self._connecting:
            self._cancel_connect_timer()
            return
        self._cancel_connect_timer()
        self._connecting = False
        self._write_to_tab("tab-broadcast", f"[bold red]Connection failed: {error}[/]")
        self.sub_title = "Disconnected"
        self._connection_timer = self.set_timer(1.5, self.app.pop_screen)

    def _cancel_connect_timer(self) -> None:
        if self._connect_timer:
            self._connect_timer.stop()
            self._connect_timer = None

    def _update_subtitle(self) -> None:
        name = self._device.name or self._device.address
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
            FAVORITES_FILE.write_text(json.dumps({"favorites": sorted(self._favorites)}, indent=2))
        except Exception:
            pass

    def _populate_channels(self) -> None:
        iface = self._interface
        if not iface or not iface.localNode or not iface.localNode.channels:
            return

        try:
            ch_list = self.query_one("#channel-list", ListView)
        except Exception:
            return
        ch_list.clear()

        try:
            for ch in iface.localNode.channels:
                if ch.role == channel_pb2.Channel.DISABLED:
                    continue
                role = "P" if ch.role == channel_pb2.Channel.PRIMARY else "S"
                name = ch.settings.name or f"CH{ch.index}"
                label = Label(f"[{role}] {name}  (ch {ch.index})")
                item = ListItem(label)
                item.data = ("channel", ch.index)
                ch_list.append(item)
        except Exception:
            return

    def _populate_nodes(self) -> None:
        iface = self._interface
        if not iface or not iface.nodes:
            return

        try:
            node_list = self.query_one("#node-list", ListView)
        except Exception:
            return
        node_list.clear()

        try:
            known = sorted(
                iface.nodes.values(),
                key=lambda n: (
                    n.get("user", {}).get("id", "") not in self._favorites,
                    n.get("user", {}).get("longName", ""),
                ),
            )
        except Exception:
            return

        try:
            for node in known:
                user = node.get("user", {})
                nid = user.get("id", "?")
                short = user.get("shortName", "?")
                long_name = user.get("longName", "?")
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

    def _on_text_msg(self, packet, interface) -> None:  # noqa: ARG001
        self._call_on_main(self._display_packet, packet)

    def _on_node_updated(self, node, interface) -> None:  # noqa: ARG001
        self._call_on_main(self._populate_nodes)

    def _on_disconnected(self, interface, **kwargs) -> None:  # noqa: ARG001
        self._call_on_main(self._handle_disconnect, "Device disconnected")

    def _call_on_main(self, callback, *args, **kwargs):
        try:
            self.app.call_from_thread(callback, *args, **kwargs)
        except RuntimeError:
            callback(*args, **kwargs)

    def _handle_disconnect(self, reason: str) -> None:
        self._connected = False
        self._write_to_tab("tab-broadcast", f"[bold yellow]{reason}[/]")
        self.sub_title = "Disconnected"

    def _display_packet(self, packet: dict) -> None:
        if not self._connected:
            return

        from_id = packet.get("fromId", "?")
        text = packet.get("decoded", {}).get("text", "")
        rx_snr = packet.get("rxSnr")
        ts = packet.get("rxTime", 0)
        ch = packet.get("channel", 0)
        to_id = packet.get("toId", "")

        tm = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else time.strftime("%H:%M:%S")

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
            long_name = event.item.data[2] if len(event.item.data) > 2 else value
            short_name = event.item.data[3] if len(event.item.data) > 3 else ""
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
        if not text or not self._connected or self._interface is None:
            return

        try:
            self._interface.sendText(
                text,
                destinationId=self._destination_id or "^all",
                channelIndex=self._channel_index,
            )
        except Exception as exc:
            self._write_to_tab("tab-broadcast", f"[bold red]Send failed: {exc}[/]")
            return

        tabs = self.query_one("#chat-tabs", TabbedContent)
        active_id = tabs.active
        tm = time.strftime("%H:%M:%S")
        entry = Text.assemble()
        entry.append(f"{tm} ", "dim")
        if self._destination_id:
            entry.append(f"[Me -> {self._destination_id}] ", "bold green")
        else:
            entry.append(f"[Me (ch{self._channel_index})] ", "bold green")
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

    def _cleanup(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._cancel_connect_timer()
        if self._connection_timer:
            self._connection_timer.stop()
            self._connection_timer = None
        try:
            pub.unsubscribe(self._on_text_msg, "meshtastic.receive.text")
            pub.unsubscribe(self._on_disconnected, "meshtastic.connection.lost")
            pub.unsubscribe(self._on_node_updated, "meshtastic.node.updated")
            pub.unsubscribe(
                self._on_connection_established, "meshtastic.connection.established"
            )
        except Exception:
            pass
        if self._interface:
            iface = self._interface
            self._interface = None
            self._connected = False
            t = threading.Thread(target=iface.close, daemon=True)
            t.start()
            t.join(timeout=5)

    def on_unmount(self) -> None:
        self._cleanup()


class MeshtasticTUI(App):
    TITLE = "Meshtastic TUI"
    SUB_TITLE = "BLE Chat"
    SCREENS: ClassVar = {"device": DeviceScreen}

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


def main():
    app = MeshtasticTUI()
    app.run()


if __name__ == "__main__":
    main()

# Meshtastic TUI

A terminal-based chat client for Meshtastic devices over BLE, built with [Textual](https://textual.textualize.io/).

## Features

- Scan for nearby Meshtastic BLE devices and connect to one
- Chat via **Broadcast** (default), individual **channels**, or direct messages to specific **nodes**
- Tabs auto-create for channels and DMs as messages arrive or when selected from the sidebar
- Favorite nodes — starred nodes sort to the top of the node list
- Key bindings for quick navigation
- **Background daemon** — the BLE connection runs in a separate process and stays alive even if the TUI restarts
- **System tray icon** — the daemon shows a tray icon indicating connection status, with options to connect/disconnect or quit
- **Automatic reconnection** — if the BLE connection drops, the daemon retries up to 5 times with exponential backoff
- **Message history** — the daemon caches up to 500 recent messages and replays them when the TUI reconnects
- **Signal strength display** — the device scan shows RSSI bars and dBm values for each device
- **Node details** — the sidebar shows battery level, SNR, and hop count for each node

## Requirements

- Python 3.11+
- A Meshtastic device with BLE support
- Bluetooth adapter on the host machine (Linux, macOS, or Windows)
- `pystray` and `Pillow` for the system tray icon (Linux may also need a tray-capable desktop environment)

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

The launcher automatically starts a **background daemon** (`meshtasticd`) that manages the BLE connection, then opens the TUI. When you quit the TUI, the daemon shuts down by default.

### Options

```bash
python main.py --daemonize          # Keep daemon running after TUI exits
python main.py --connect <BLE_ADDR> # Auto-connect to a BLE device on startup
python main.py --help               # Show full help
```

Use `--daemonize` if you want the daemon to keep the BLE connection alive between TUI sessions. The next time you run `python main.py`, it will detect the running daemon and connect to it.

### Screens

1. **Device Scan** — Lists nearby Meshtastic BLE devices. Select one to connect.
2. **Chat** — Main chat interface with a sidebar (channels + nodes) and a tabbed message area.

## Key Bindings

| Key       | Action                     |
|-----------|----------------------------|
| `q`       | Quit                       |
| `Escape`  | Disconnect and go back     |
| `c`       | Focus channels list        |
| `n`       | Focus nodes list           |
| `b`       | Switch to Broadcast tab    |
| `f`       | Toggle favorite on node    |
| `Ctrl+W`  | Close current tab          |
| `Enter`   | Send message (in input)    |

### Sidebar Interaction

- **Channels** — Primary (`[P]`) and secondary (`[S]`) channels. Select one to open (or switch to) its tab.
- **Nodes** — All known nodes, sorted by favorites first. A ★ marks a favorite. Select a node to open a DM tab.
- **Node list header** shows the total known node count.

## Architecture

The application is split into two processes:

- **`daemon.py`** (background) — Maintains the BLE connection to the Meshtastic device, subscribes to events, and relays them over a Unix socket.
- **`app.py`** (TUI) — The Textual-based terminal interface. Connects to the daemon over the Unix socket.
- **`protocol.py`** (shared) — Defines the `Message` dataclass and JSON socket protocol used by both processes.

They communicate using newline-delimited JSON over a Unix domain socket at `~/.config/meshtastic-tui/meshtasticd.sock`.

## Configuration

- Favorites are persisted to `~/.config/meshtastic-tui/favorites.json`.
- Daemon logs are written to `~/.config/meshtastic-tui/daemon.log`.

## Disclaimer

This is an unofficial, community-built TUI client. Not affiliated with the Meshtastic project.

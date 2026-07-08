# Meshtastic TUI

A terminal-based chat client for Meshtastic devices over BLE, built with [Textual](https://textual.textualize.io/).

## Features

- Scan for nearby Meshtastic BLE devices and connect to one
- Chat via **Broadcast** (default), individual **channels**, or direct messages to specific **nodes**
- Tabs auto-create for channels and DMs as messages arrive or when selected from the sidebar
- Favorite nodes — starred nodes sort to the top of the node list
- Key bindings for quick navigation

## Requirements

- Python 3.11+
- A Meshtastic device with BLE support
- Bluetooth adapter on the host machine (Linux, macOS, or Windows)

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python app.py
```

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
| `Tab`     | Cycle between tabs         |

### Sidebar Interaction

- **Channels** — Primary (`[P]`) and secondary (`[S]`) channels. Select one to open (or switch to) its tab.
- **Nodes** — All known nodes, sorted by favorites first. A ★ marks a favorite. Select a node to open a DM tab.
- **Node list header** shows the total known node count.

## Configuration

Favorites are persisted to `~/.config/meshtastic-tui/favorites.json`.

## Disclaimer

This is an unofficial, community-built TUI client. Not affiliated with the Meshtastic project.

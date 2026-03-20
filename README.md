# SysControl

An AI agent for your Mac that answers questions about your system — and can extend itself with new tools on the fly.

57 real-time tools covering CPU, RAM, GPU, disk, network, processes, iMessage, clipboard, browser, weather, reminders, Docker, Time Machine, Wi-Fi, calendar, contacts, shell, and more. The agent picks the right tools automatically, runs them in parallel, and answers in plain English.

Two ways to run it:

- **Terminal agent** (`agent.py`) — conversational REPL powered by Ollama (local) or Ollama Cloud
- **Claude Desktop** — connect `mcp/server.py` directly via MCP

---

## Requirements

- Python **3.11** or later
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager
- [Ollama](https://ollama.com) (local mode) **or** an Ollama Cloud API key (cloud mode)

---

## Installation

```bash
git clone https://github.com/ks6573/SysControl.git
cd SysControl

# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync
```

---

## Terminal Agent

```bash
uv run agent.py
```

### CLI Flags

```bash
uv run agent.py                                          # interactive
uv run agent.py --provider local --model qwen2.5        # local, skip prompt
uv run agent.py --provider cloud --api-key sk-...       # cloud, skip prompt
```

### Local Mode (Ollama)

```bash
ollama pull qwen2.5   # recommended
ollama serve
uv run agent.py --provider local
```

**Tool-calling capable models:**

| Model | Notes |
|---|---|
| `qwen2.5` | Default. Best tool use at 7B |
| `qwen3:8b` | Newer, includes thinking mode |
| `llama3.1:8b` | Battle-tested alternative |
| `mistral` | Lightweight and fast |

> Models without native tool-calling (e.g. `gemma3`) will error.

### Cloud Mode (Ollama Cloud)

```bash
uv run agent.py --provider cloud
# Enter your key when prompted — not echoed or stored in shell history
```

Get a key at [ollama.com/settings/keys](https://ollama.com/settings/keys). Default cloud model: `gpt-oss:120b`.

### Ending a Session

Say any natural goodbye (`bye`, `exit`, `quit`, `done`, `farewell`, `cya`, `goodnight`, …) or press **Ctrl-C**. The agent will offer to save your session before exiting.

### Session Memory

On exit you are prompted:

```
Save session? [yes/no/md/txt]:
```

- `yes` / `md` — appends the conversation as a Markdown section to `SysControl_Memory.md`
- `txt` — plain text instead
- `no` — nothing written

On next startup, if the file exists its contents are injected into the system prompt so the agent has context from prior sessions. The file is append-only and plain text — edit or delete entries freely.

> **Privacy:** SysControl stores only what you explicitly save. Ollama processes queries locally by default.

---

## Permissions & Security

Sensitive tools are **disabled by default**. Enable them in `~/.syscontrol/config.json`:

```json
{
  "allow_shell":           true,
  "allow_messaging":       true,
  "allow_message_history": true,
  "allow_screenshot":      true,
  "allow_file_read":       true,
  "allow_file_write":      true,
  "allow_calendar":        true,
  "allow_contacts":        true,
  "allow_accessibility":   true,
  "allow_tool_creation":   true
}
```

Each disabled tool returns an error with the exact flag needed to enable it.

---

## Claude Desktop Setup

**1. Add the MCP server to your config**

| Platform | Config path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "system-monitor": {
      "command": "/path/to/uv",
      "args": ["run", "/absolute/path/to/SyscontrolMCP/mcp/server.py"],
      "env": {}
    }
  }
}
```

Use `which uv` to get the uv path.

**2. Set the system prompt** — create a Claude Desktop Project and paste the contents of `mcp/prompt.json` into the Project Instructions field.

**3. Restart Claude Desktop** — `system-monitor` will appear in the MCP servers list.

---

## Self-Extension

When you ask for something no tool covers, the agent offers to build it:

```
You: What song is playing in Spotify right now?

Agent: I don't have a tool for that. Want me to create one? (yes/no)

You: yes

Agent: ✓ Tool `get_spotify_track` installed. Restart and ask again.
```

The agent writes a Python function, validates syntax, scans for dangerous patterns (`eval`, `exec`, etc.), and appends it to `mcp/server.py`. Requires:

```json
{ "allow_tool_creation": true }
```

---

## Tools (57 total)

### Monitoring

| Tool | What it does |
|---|---|
| `get_cpu_usage` | CPU load (total + per-core), clock frequency, inline bar chart |
| `get_ram_usage` | RAM and swap — used, available, percent, inline stacked chart |
| `get_gpu_usage` | GPU load, VRAM, temperature per device (NVIDIA/pynvml), inline chart |
| `get_disk_usage` | Per-partition space and cumulative I/O counters |
| `get_network_usage` | Cumulative bytes sent/received and per-interface status |
| `get_realtime_io` | Live disk read/write and network download/upload speed (MB/s) |
| `get_top_processes` | Top N processes by CPU or memory |
| `get_full_snapshot` | Single call: CPU + RAM + GPU + disk + network + top processes |
| `get_system_alerts` | Triage scan returning prioritized critical/warning alerts |

### System & Hardware

| Tool | What it does |
|---|---|
| `get_device_specs` | Static profile: CPU model, core count, RAM, GPU VRAM, disks, OS |
| `get_battery_status` | Percent, charging state, time remaining |
| `get_temperature_sensors` | CPU/motherboard sensors (Linux/Windows) |
| `get_system_uptime` | Boot time, uptime, 1/5/15-min load averages |
| `get_hardware_profile` | Live pressure + specs + OC capability + upgrade feasibility + bottleneck analysis |

### Process Management

| Tool | What it does |
|---|---|
| `get_process_details` | Deep inspection of a PID: path, cmdline, user, RSS/VMS, threads, open files |
| `search_process` | Find processes by name (case-insensitive partial match) |
| `kill_process` | SIGTERM (default) or SIGKILL a PID. Refuses critical system processes. |

### Network & Connectivity

| Tool | What it does |
|---|---|
| `get_network_connections` | All active TCP/UDP connections with state and owning process |
| `network_latency_check` | Pings gateway, Cloudflare, Google DNS in parallel and diagnoses slowness |
| `get_wifi_networks` | Nearby networks with SSID, channel, security, signal strength |

### Storage

| Tool | What it does |
|---|---|
| `find_large_files` | Top N largest files under a path. Skips `.git`, `node_modules`, `.venv` |
| `eject_disk` | Unmount and eject an external disk by mountpoint |

### Messaging & Communication

| Tool | What it does |
|---|---|
| `send_imessage` | Send an iMessage or SMS via Messages.app. macOS only. |
| `get_imessage_history` | Read recent messages from `~/Library/Messages/chat.db`. macOS only. |

### Browser & Web

| Tool | What it does |
|---|---|
| `web_search` | DuckDuckGo search — title, URL, snippet. No API key. |
| `web_fetch` | Fetch a URL as plain text. No browser required. |
| `grant_browser_access` | Unlock browser control (called once, after user consent) |
| `browser_open_url` | Open a URL in the default browser |
| `browser_navigate` | Navigate the active tab via AppleScript (macOS) |
| `browser_get_page` | Return the URL, title, and text of the current tab (macOS) |

### Clipboard & Screen

| Tool | What it does |
|---|---|
| `get_clipboard` | Return current clipboard text |
| `set_clipboard` | Write text to the clipboard |
| `take_screenshot` | Full-screen PNG returned inline. Optionally save to file. macOS only. |

### App Control & System

| Tool | What it does |
|---|---|
| `open_app` | Open an app by name (`open -a`). macOS only. |
| `quit_app` | Gracefully quit (AppleScript) or force-kill an app. macOS only. |
| `get_volume` | Output, input, and alert volume; mute state |
| `set_volume` | Set system output volume (0–100) |
| `get_frontmost_app` | Return the name of the focused application |
| `toggle_do_not_disturb` | Enable/disable Focus / DnD |
| `run_shortcut` | Run a named Shortcut via `shortcuts run`. macOS 12+. |

### File I/O & Shell

| Tool | What it does |
|---|---|
| `read_file` | Read a text file (up to 32,000 chars) |
| `write_file` | Write text to any path, creating directories as needed |
| `run_shell_command` | Execute a bash command and return stdout/stderr. **Disabled by default.** |

### Calendar, Contacts & Logs

| Tool | What it does |
|---|---|
| `get_calendar_events` | Upcoming events from Calendar.app for the next N days. macOS only. |
| `get_contact` | Search Contacts.app by name — phone and email. macOS only. |
| `get_startup_items` | Auto-start items (macOS LaunchAgents, Windows Registry, Linux `.desktop`) |
| `tail_system_logs` | Last N lines of the system log with optional keyword filter |

### Utilities

| Tool | What it does |
|---|---|
| `set_reminder` | Schedule a macOS notification. Accepts `"in 2 hours"`, `"tomorrow at 9am"`, etc. |
| `list_reminders` | All pending reminders with IDs and fire times |
| `cancel_reminder` | Cancel a reminder by ID |
| `get_weather` | Current weather + clothing recommendations. Auto-detects location from IP. |
| `check_app_updates` | Homebrew, Mac App Store, and system software updates. macOS only. |
| `get_docker_status` | Running containers with live CPU%, memory, image, status, and ports |
| `get_time_machine_status` | Last backup time, phase and progress if running, destination. macOS only. |
| `track_package` | Track UPS, USPS, FedEx, or DHL shipments by tracking number |

### Self-Extension

| Tool | What it does |
|---|---|
| `create_tool` | Write, validate, and install a new MCP tool into `server.py`. Requires `allow_tool_creation`. |
| `list_user_tools` | List all tools installed via `create_tool` |

---

## Overclocking Support

Detected automatically from hardware and platform:

| Platform | CPU OC | GPU OC |
|---|---|---|
| Apple Silicon (M-series) | ✗ Not supported | ✗ Not supported |
| Intel Mac | ✗ Not supported (no BIOS) | ✗ Not supported (macOS) |
| Intel K/KF/KS — Windows/Linux | ✅ Intel XTU or BIOS | ✅ MSI Afterburner |
| AMD Ryzen — Windows/Linux | ✅ Ryzen Master / PBO | ✅ MSI Afterburner |

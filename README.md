# Syscontrol

An AI-powered system monitoring agent built on the [Model Context Protocol (MCP)](https://modelcontextprotocol.io). Gives a local or cloud LLM real-time access to **36 system tools** — CPU, RAM, GPU, disk, network, processes, Docker, Time Machine, browser control, weather, package tracking, and more — then delivers context-aware optimization advice, upgrade recommendations, and workload-specific guidance.

Three ways to use it:

- **Agentic terminal agent** (`agent.py`) — conversational REPL powered by a local Ollama model or Ollama Cloud
- **Remote messaging bridge** (`remote.py`) — control your Mac from Telegram, WhatsApp, or Messenger via a phone
- **Claude Desktop integration** — connect `mcp/server.py` directly to Claude Desktop via MCP

---

## Requirements

- Python **3.11** or later
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager
- [Ollama](https://ollama.com) (for local mode) **or** an Ollama Cloud API key (for cloud mode)

---

## Installation

```bash
# 1. Clone
git clone https://github.com/yourname/SyscontrolMCP.git
cd SyscontrolMCP

# 2. Install uv (skip if already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Install dependencies
uv sync
```

---

## Agentic Terminal Agent

`agent.py` is a streaming, tool-calling conversational REPL. The model autonomously selects and calls the right tools to answer your questions.

### Quick Start

```bash
uv run agent.py
```

### CLI Flags

All flags are optional. When omitted, you'll be prompted interactively.

```
usage: agent.py [-h] [--provider {cloud,local}] [--model MODEL] [--api-key KEY]

Options:
  --provider {cloud,local}   Skip the interactive prompt and use this provider directly
  --model MODEL              Override the default model for the chosen provider
  --api-key KEY              Ollama API key for cloud (skips the interactive prompt)
```

**Examples:**

```bash
# Interactive (default)
uv run agent.py

# Non-interactive local
uv run agent.py --provider local --model qwen2.5

# Non-interactive cloud
uv run agent.py --provider cloud --api-key sk-...

# CI / scripted use
uv run agent.py --provider cloud --model gpt-oss:120b --api-key "$OLLAMA_KEY"
```

### Local Mode (Ollama)

Requires [Ollama](https://ollama.com) running locally. Pull a model that supports tool calling:

```bash
ollama pull qwen2.5   # recommended default
ollama serve          # ensure Ollama is running
```

**Recommended local models (tool-calling capable):**

| Model | Pull command | Notes |
|---|---|---|
| `qwen2.5` | `ollama pull qwen2.5` | Default. Best tool use at 7B |
| `qwen3:8b` | `ollama pull qwen3:8b` | Newer, includes thinking mode |
| `llama3.1:8b` | `ollama pull llama3.1:8b` | Battle-tested alternative |
| `mistral` | `ollama pull mistral` | Lightweight and fast |

> Models without tool-calling support (e.g. `gemma3`) will error. Stick to the list above.

To change the default local model, edit `agent/cli.py`:
```python
LOCAL_MODEL = "qwen2.5"
```

### Cloud Mode (Ollama Cloud)

Runs `gpt-oss:120b` via [Ollama Cloud](https://ollama.com). Get an API key at [ollama.com/settings/keys](https://ollama.com/settings/keys).

```bash
# Interactive — key is entered securely (not shown on screen, not in shell history)
uv run agent.py --provider cloud

# Non-interactive
uv run agent.py --provider cloud --api-key sk-your-key-here
```

### Example Prompts

```
My Mac feels sluggish — what's going on?
Give me a full system snapshot
Which process is using the most memory?
What's actually eating my disk space?
Is my internet slow right now, and where's the bottleneck?
What Docker containers are running and how much memory are they using?
When did Time Machine last back up my Mac?
What should I wear today?
What's connecting to the internet from my machine?
I'm running Docker and VS Code — how can I optimize RAM?
I want faster Lightroom exports. What should I upgrade?
Remind me in 2 hours to check on my download
Show me the last 50 system log lines filtered for errors
Track my UPS package 1Z999AA10123456784
```

---

## Remote Messaging Bridge

`remote.py` exposes the full agent to **Telegram**, **WhatsApp**, and **Facebook Messenger** simultaneously through a single webhook server. Control your Mac from your phone — no port-forwarding needed.

### Setup

**1. Install Cloudflare Tunnel** (one-time)
```bash
brew install cloudflared
```

**2. First run — create config**
```bash
uv run remote.py
# Exits and creates ~/.syscontrol/remote_config.json — fill in your tokens
```

**3. Fill in `~/.syscontrol/remote_config.json`**
```json
{
  "provider": "local",
  "model": "qwen2.5",
  "api_key": "ollama",
  "base_url": "http://localhost:11434/v1",
  "allowed_chat_ids": {
    "telegram": [123456789]
  },
  "telegram": {
    "enabled": true,
    "token": "YOUR_BOT_TOKEN"
  }
}
```

> **Security:** `remote_config.json` is gitignored and lives in `~/.syscontrol/` — never in the repo. `allowed_chat_ids` is enforced on every incoming message; messages from unknown IDs are rejected and the sender's ID is logged so you can add it.

**4. Start a Cloudflare Tunnel** (in a separate terminal)
```bash
cloudflared tunnel --url http://127.0.0.1:8080
# Copy the https://xxxx.trycloudflare.com URL
```

**5. Register the Telegram webhook**
```bash
uv run remote.py --register-telegram https://xxxx.trycloudflare.com
```

**6. Start the bridge**
```bash
uv run remote.py
```

### Finding Your Telegram chat_id

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token into your config
2. Send any message to your bot
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
4. Find `"id"` inside the `"chat"` object — that's your `chat_id`
5. Add it to `allowed_chat_ids.telegram` in your config

### Remote Bridge Options

```
usage: remote.py [-h] [--port PORT] [--host HOST] [--register-telegram TUNNEL_URL]

Options:
  --port PORT                     Port to listen on (default: 8080)
  --host HOST                     Host to bind to (default: 127.0.0.1)
  --register-telegram TUNNEL_URL  Register the Telegram webhook with the given public URL, then exit
```

---

## Claude Desktop Setup

**1. Locate your config file**

| Platform | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

**2. Add the MCP server block**

```json
{
  "mcpServers": {
    "system-monitor": {
      "command": "/Users/yourname/.local/bin/uv",
      "args": ["run", "/absolute/path/to/SyscontrolMCP/mcp/server.py"],
      "env": {}
    }
  }
}
```

Use `which uv` to find your `uv` binary path.

**3. Set the system prompt**

Create a new Project in Claude Desktop and paste the value of `system_prompt.prompt` from `mcp/prompt.json` into the Project Instructions field.

**4. Restart Claude Desktop**

`system-monitor` will appear in the connected MCP servers list.

---

## Tools (36 total)

### Live Metrics

| Tool | Description |
|---|---|
| `get_cpu_usage` | CPU load (total + per-core), core count, clock frequency. Includes an inline bar chart. |
| `get_ram_usage` | RAM and swap — total, used, available, percent. Includes an inline stacked bar chart. |
| `get_gpu_usage` | GPU load, VRAM, and temperature per device (requires `gputil`). Includes a grouped bar chart. |
| `get_disk_usage` | Per-partition disk space and cumulative I/O counters since boot. |
| `get_network_usage` | Cumulative bytes sent/received and per-interface status. |
| `get_realtime_io` | **Instantaneous** disk read/write and network download/upload speed (MB/s). Use this instead of `get_disk_usage` / `get_network_usage` for current throughput. |
| `get_top_processes` | Top N processes by CPU or memory. Accepts `n` (default 10) and `sort_by` (`cpu` \| `memory`). |
| `get_full_snapshot` | Single call combining CPU, RAM, GPU, disk, network, and top processes. |

### System & Hardware Info

| Tool | Description |
|---|---|
| `get_device_specs` | Static hardware profile: CPU model, core count, total RAM, GPU VRAM, disk capacities, OS. |
| `get_battery_status` | Battery percent, charging state, and estimated time remaining. |
| `get_temperature_sensors` | CPU and motherboard temperature sensors (Linux/Windows). On macOS, explains the limitation and suggests alternatives. |
| `get_system_uptime` | Boot timestamp, uptime (days/hours/minutes), and 1/5/15-min load averages. |
| `get_system_alerts` | Triage scan of all key metrics. Returns prioritized critical/warning alerts. **Use this first for "why is my machine slow?" questions.** |

### Process Management

| Tool | Description |
|---|---|
| `get_process_details` | Deep inspection of a PID: path, command line, user, RSS/VMS, thread count, open files. |
| `search_process` | Find running processes by name (case-insensitive partial match). |
| `kill_process` | Terminate a process by PID. SIGTERM by default; SIGKILL with `force=true`. Refuses to kill critical system processes. |

### Network

| Tool | Description |
|---|---|
| `get_network_connections` | All active TCP/UDP connections with local/remote address, state, and owning process. |
| `network_latency_check` | Pings your gateway, Cloudflare (1.1.1.1), and Google DNS (8.8.8.8) **in parallel**, then diagnoses where slowness is introduced (router / ISP / congestion). |

### Startup & Logs

| Tool | Description |
|---|---|
| `get_startup_items` | Auto-start items at login (macOS LaunchAgents, Windows Registry Run keys, Linux `.desktop` files). |
| `tail_system_logs` | Last N lines from the system log (macOS unified log or Linux journalctl). Optional keyword filter. |

### Storage

| Tool | Description |
|---|---|
| `find_large_files` | Top N largest files under a path. Skips `.git`, `node_modules`, `.venv`, etc. Use to find what's eating disk space. |

### Hardware Advisor

| Tool | Description |
|---|---|
| `get_hardware_profile` | Live pressure + static specs + OC capability + per-component upgrade feasibility + workload bottleneck analysis. Accepts a `use_case` string (e.g. `"lightroom"`, `"gaming"`, `"docker"`). |

Supported workloads: Lightroom / photo editing, video editing (Premiere, DaVinci, Final Cut), gaming, 3D rendering (Blender), compilation / Xcode, Docker / VMs, machine learning, streaming.

### Browser & Web

| Tool | Description |
|---|---|
| `web_fetch` | Fetch a URL and return plain text (no browser required). |
| `web_search` | DuckDuckGo search returning title, URL, and snippet for each result. No API key needed. |
| `grant_browser_access` | Enable browser control tools (called once after user consent). |
| `browser_open_url` | Open a URL in the default browser. |
| `browser_navigate` | Navigate the active tab to a URL via AppleScript (macOS). |
| `browser_get_page` | Return the URL, title, and visible text of the current browser tab (macOS). |

### Utilities

| Tool | Description |
|---|---|
| `set_reminder` | Schedule a macOS notification. Accepts natural language: `"in 2 hours"`, `"tomorrow at 9am"`. |
| `list_reminders` | List all pending reminders with IDs and fire times. |
| `cancel_reminder` | Cancel a pending reminder by its ID. |
| `get_weather` | Current weather + clothing recommendations. Auto-detects location from IP or accepts a city name. Supports `imperial` / `metric`. |
| `check_app_updates` | macOS only. Checks Homebrew (formulae + casks), Mac App Store (`mas`), and system software for updates. |
| `get_docker_status` | Running containers with live CPU%, memory usage, image, status, and ports. Returns a clear error if Docker is not running. |
| `get_time_machine_status` | macOS Time Machine: last backup time and age, current phase and progress if running, and destination drive. |
| `track_package` | Track a UPS, USPS, FedEx, or DHL shipment by tracking number. Auto-detects carrier. |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  agent.py (shim)         remote.py (shim)                   │
│       │                        │                            │
│       ▼                        ▼                            │
│  agent/cli.py            agent/remote.py                    │
│  Streaming REPL          FastAPI webhook server             │
│  (local / cloud)         Telegram · WhatsApp · Messenger    │
│       │                        │                            │
│       └──────────┬─────────────┘                            │
│                  │  agent/core.py                           │
│                  │  MCPClientPool (up to 4 workers)         │
│                  │  ThreadPoolExecutor: parallel tool calls  │
└──────────────────┼──────────────────────────────────────────┘
                   │ JSON-RPC 2.0 over stdio
                   ▼
┌─────────────────────────────────────────────────────────────┐
│                      mcp/server.py                          │
│                                                             │
│  36 tools  ─  psutil, matplotlib, subprocess, urllib        │
│  ReminderChecker background thread (15s polling)            │
└─────────────────────────────────────────────────────────────┘
```

**Key design decisions:**

| Feature | Detail |
|---|---|
| **Parallel tool execution** | `MCPClientPool` spawns up to 4 `mcp/server.py` subprocesses. When the LLM calls multiple tools in one turn, they run concurrently via `ThreadPoolExecutor`. |
| **Parallel startup** | MCP client initialization and `mcp/prompt.json` loading happen in parallel threads — shaves ~200ms off startup. |
| **Internally parallel tools** | `network_latency_check` pings 4 targets simultaneously. `get_time_machine_status` runs 3 `tmutil` calls simultaneously. |
| **Buffered streaming** | Token fragments are collected in a list and joined once, avoiding O(n²) string copies during long responses. |
| **Graceful shutdown** | `MCPClient.close()` sends SIGTERM → waits 2s → SIGKILL, preventing zombie `mcp/server.py` processes. |
| **Secure API key input** | `getpass.getpass()` for interactive prompts — key never echoed to the terminal or stored in shell history. |
| **Remote session isolation** | Each `(platform, chat_id)` pair maintains its own message history; history length is trimmed in a thread-safe way. |

---

## Overclocking Support

Detected automatically based on hardware and platform:

| Platform | CPU OC | GPU OC |
|---|---|---|
| Apple Silicon (M-series) | ✗ Not supported | ✗ Not supported |
| Intel Mac | ✗ Not supported (no BIOS) | ✗ Not supported (macOS) |
| Intel K/KF/KS — Windows/Linux | ✅ Intel XTU or BIOS | ✅ MSI Afterburner |
| AMD Ryzen — Windows/Linux | ✅ Ryzen Master / PBO | ✅ MSI Afterburner |

---

## Project Structure

```
SyscontrolMCP/
├── agent.py                   # Entry-point shim → agent/cli.py
├── remote.py                  # Entry-point shim → agent/remote.py
│
├── agent/
│   ├── core.py                # MCPClient, MCPClientPool, shared helpers
│   ├── cli.py                 # Streaming agentic REPL (local or cloud LLM)
│   └── remote.py              # Telegram / WhatsApp / Messenger bridge
│
├── mcp/
│   ├── server.py              # MCP server — 36 tools, JSON-RPC dispatcher
│   └── prompt.json            # System prompt (paste into Claude Desktop Projects)
│
├── claude_desktop_config.json # Ready-to-use Claude Desktop config (update paths)
├── pyproject.toml             # Project metadata and dependencies (uv)
└── uv.lock                    # Pinned dependency versions
```

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `psutil` | ≥ 5.9.0 | System metrics (CPU, RAM, disk, network, processes) |
| `gputil` | ≥ 1.4.0 | GPU metrics — gracefully disabled on Apple Silicon |
| `matplotlib` | ≥ 3.7.0 | Inline chart generation for CPU, RAM, and GPU tools |
| `openai` | ≥ 2.26.0 | OpenAI-compatible client for Ollama local and cloud |
| `fastapi` | ≥ 0.110.0 | Webhook server for the remote messaging bridge |
| `uvicorn` | ≥ 0.29.0 | ASGI server for FastAPI |
| `httpx` | ≥ 0.27.0 | Async HTTP client for outbound messaging API calls |

Install dev tools (ruff, mypy, pytest):

```bash
uv sync --extra dev
```

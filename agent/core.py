#!/usr/bin/env python3
"""
SysControl Agent — Core utilities.

Provides the MCP client, client pool, and shared helpers used by both the
CLI (agent/cli.py) and the remote bridge (agent/remote.py).
"""

import json
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

from openai import OpenAI  # noqa: F401 — re-exported for downstream imports

# ── Constants ─────────────────────────────────────────────────────────────────

# agent/core.py lives inside agent/, so go up one level to reach mcp/
SERVER_PATH = Path(__file__).parent.parent / "mcp" / "server.py"
PROMPT_PATH = Path(__file__).parent.parent / "mcp" / "prompt.json"
MAX_TOKENS         = 16384
POOL_SIZE          = 4          # max parallel MCP worker processes
MAX_PARALLEL_TOOLS = POOL_SIZE  # batch size capped to pool capacity

# ── Provider config ───────────────────────────────────────────────────────────

CLOUD_MODEL    = "gpt-oss:120b"
CLOUD_BASE_URL = "https://ollama.com/v1"

LOCAL_MODEL    = "qwen3:30b"  # any model pulled via: ollama pull <model>
LOCAL_BASE_URL = "http://localhost:11434/v1"
LOCAL_API_KEY  = "ollama"   # Ollama doesn't require a real key

# ANSI colours — only emitted when stdout is a real terminal.
_USE_COLOR = sys.stdout.isatty()
RESET   = "\033[0m"  if _USE_COLOR else ""
BOLD    = "\033[1m"  if _USE_COLOR else ""
DIM     = "\033[2m"  if _USE_COLOR else ""
CYAN    = "\033[36m" if _USE_COLOR else ""
GREEN   = "\033[32m" if _USE_COLOR else ""
YELLOW  = "\033[33m" if _USE_COLOR else ""
BLUE    = "\033[34m" if _USE_COLOR else ""
WHITE   = "\033[97m" if _USE_COLOR else ""   # bright white — used for bold text
MAGENTA = "\033[35m" if _USE_COLOR else ""   # used for inline code

# ── MCP Client ────────────────────────────────────────────────────────────────

class MCPClient:
    """Minimal JSON-RPC client that talks to mcp/server.py over stdio."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._id   = 0
        self._lock = threading.Lock()   # serialise writes/reads on this pipe
        self._initialize()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            msg: dict = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
            if params:
                msg["params"] = params
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
            raw = self.proc.stdout.readline()
            if not raw:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(
                    f"MCP server closed unexpectedly."
                    f"{(' Server error: ' + err.strip()) if err.strip() else ''}"
                )
            return json.loads(raw)

    def _notify(self, method: str) -> None:
        with self._lock:
            msg = {"jsonrpc": "2.0", "method": method}
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def _initialize(self) -> None:
        self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities":    {},
            "clientInfo":      {"name": "syscontrol-agent", "version": "1.0"},
        })
        self._notify("initialized")

    def list_tools(self) -> list[dict]:
        resp = self._send("tools/list")
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        resp    = self._send("tools/call", {"name": name, "arguments": arguments or {}})
        content = resp.get("result", {}).get("content", [])
        return content[0]["text"] if content else str(resp)

    def close(self) -> None:
        """Gracefully shut down the subprocess: close stdin → wait → kill."""
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass


# ── MCP Client Pool ───────────────────────────────────────────────────────────

class MCPClientPool:
    """
    Manages a pool of MCPClient instances so independent tool calls can be
    executed concurrently — each call gets its own subprocess/pipe.

    Workers are lazily initialised: the primary client is created eagerly and
    extras are spawned only when a parallel batch actually needs them.
    """

    def __init__(self, primary: MCPClient, pool_size: int = POOL_SIZE):
        self._clients: list[MCPClient] = [primary]
        self._pool_size = pool_size
        self._pool_lock = threading.Lock()
        self._parallel_safe: set[str] | None = None  # lazily populated

    def _get_or_create(self, index: int) -> MCPClient:
        # Fast path — already have enough clients.
        with self._pool_lock:
            if len(self._clients) > index:
                return self._clients[index]

        # Construct the new client OUTSIDE the lock — MCPClient.__init__ spawns
        # a subprocess and runs the MCP handshake, which can take 100–200 ms.
        # Holding the lock for that entire time would block every other thread.
        new_client = MCPClient()

        with self._pool_lock:
            # Re-check under lock: another thread may have beaten us.
            # If so, discard our new_client to avoid a leaked subprocess.
            if len(self._clients) > index:
                new_client.close()
                return self._clients[index]
            self._clients.append(new_client)
            return new_client

    # Sentinel: distinguishes "server unreachable, allow everything" from a
    # legitimately loaded (but possibly empty) set of safe tool names.
    _FALLBACK: frozenset = frozenset()

    def _get_parallel_safe(self) -> frozenset | set[str]:
        """Return the set of tool names that are safe to run concurrently.

        Lazily fetches the tool list from the primary MCP client on first call
        and caches it for the lifetime of the pool.
        """
        if self._parallel_safe is None:
            try:
                tools = self._clients[0].list_tools()
                self._parallel_safe = {
                    t["name"] for t in tools if t.get("parallel", True)
                }
            except Exception:
                # Server unreachable — use sentinel so _is_parallel_safe
                # falls back to allowing everything (original behaviour).
                self._parallel_safe = self._FALLBACK
        return self._parallel_safe

    def _is_parallel_safe(self, name: str) -> bool:
        safe = self._get_parallel_safe()
        if safe is self._FALLBACK:
            return True   # error fallback: allow everything
        return name in safe

    def call_tools_parallel(
        self, tool_calls: list[dict]
    ) -> list[tuple[str, str, str]]:
        """
        Execute tool calls with parallel-safety enforcement.

        Batch-safe tools (read-only, fast, no side effects) run concurrently,
        capped at MAX_PARALLEL_TOOLS per batch.  Unsafe tools (blocking,
        state-mutating, or large-output) always run sequentially on the primary
        client.  Results are returned in the original request order.
        """
        if len(tool_calls) == 1:
            # Fast path: no thread overhead for a single call.
            tc   = tool_calls[0]
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            result = self._clients[0].call_tool(name, args)
            return [(tc["id"], name, result)]

        # Partition by parallel safety in a single pass, preserving original indices.
        safe_indexed: list[tuple[int, dict]]   = []
        serial_indexed: list[tuple[int, dict]] = []
        for i, tc in enumerate(tool_calls):
            (safe_indexed if self._is_parallel_safe(tc["function"]["name"])
             else serial_indexed).append((i, tc))

        results: list[tuple[int, str, str, str]] = []  # (orig_idx, tc_id, name, result)

        def _run_one(
            order: int, tc: dict, client: MCPClient
        ) -> tuple[int, str, str, str]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            return (order, tc["id"], name, client.call_tool(name, args))

        # Run parallel-safe calls in batches of at most MAX_PARALLEL_TOOLS.
        remaining = list(safe_indexed)
        while remaining:
            batch    = remaining[:MAX_PARALLEL_TOOLS]
            remaining = remaining[MAX_PARALLEL_TOOLS:]
            n_workers = min(len(batch), self._pool_size)
            with ThreadPoolExecutor(max_workers=n_workers) as ex:
                futures = [
                    ex.submit(_run_one, order, tc, self._get_or_create(slot % n_workers))
                    for slot, (order, tc) in enumerate(batch)
                ]
                for fut in as_completed(futures):
                    results.append(fut.result())

        # Run serial calls one at a time on the primary client.
        for order, tc in serial_indexed:
            results.append(_run_one(order, tc, self._clients[0]))

        results.sort(key=lambda x: x[0])
        return [(tc_id, name, result) for _, tc_id, name, result in results]

    def close_all(self) -> None:
        for client in self._clients:
            client.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    """Load and cache the system prompt — file is read once per process."""
    data = json.loads(PROMPT_PATH.read_text(encoding="utf-8"))
    return data["system_prompt"]["prompt"]


def mcp_to_openai_tools(mcp_tools: list[dict]) -> list[dict]:
    """Convert MCP tool definitions to the OpenAI/Ollama tool format."""
    return [
        {
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t.get("description", ""),
                "parameters":  t.get("inputSchema", {
                    "type":       "object",
                    "properties": {},
                    "required":   [],
                }),
            },
        }
        for t in mcp_tools
    ]


# ── Markdown → ANSI colorizer ─────────────────────────────────────────────────

# Pre-compiled patterns for speed (called on every streamed line).
_MD_HEADER   = re.compile(r"^(#{1,3})\s+(.+)$")
_MD_BOLD     = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC   = re.compile(r"\*([^*\n]+?)\*")
_MD_CODE     = re.compile(r"`([^`\n]+)`")
_MD_BULLET   = re.compile(r"^(\s*)[-•*]\s+")
_MD_NUMBERED = re.compile(r"^(\s*)(\d+)\.\s+")
_MD_HR       = re.compile(r"^[-=_]{3,}\s*$")


def _colorize(line: str) -> str:
    """
    Convert one line of markdown to ANSI-coloured plain text.
    Markers are consumed; only the coloured content is printed.
    """
    # Horizontal rule  ---  ===  ___
    if _MD_HR.match(line):
        return f"{DIM}{'─' * 56}{RESET}"

    # # / ## / ### heading
    m = _MD_HEADER.match(line)
    if m:
        level = len(m.group(1))
        prefix = "  " * (level - 1)          # indent sub-headings slightly
        return f"{prefix}{BOLD}{CYAN}{m.group(2)}{RESET}"

    # Bullet list  - item  or  • item
    m = _MD_BULLET.match(line)
    if m:
        indent = m.group(1)
        rest   = line[m.end():]  # everything after the marker
        rest   = _apply_inline(rest)
        return f"{indent}{CYAN}•{RESET} {rest}"

    # Numbered list  1. item
    m = _MD_NUMBERED.match(line)
    if m:
        indent = m.group(1)
        num    = m.group(2)
        rest   = line[m.end():]
        rest   = _apply_inline(rest)
        return f"{indent}{CYAN}{num}.{RESET} {rest}"

    # Regular line — apply inline markers only
    return _apply_inline(line)


# Pre-built replacement strings with backreferences — faster than lambdas
# because no per-call closure allocation is needed.
_BOLD_REPL   = f"{BOLD}{WHITE}\\1{RESET}"
_ITALIC_REPL = f"{YELLOW}\\1{RESET}"
_CODE_REPL   = f"{MAGENTA}\\1{RESET}"


def _apply_inline(text: str) -> str:
    """Apply bold / italic / code colour to inline spans, consuming the markers."""
    # **bold** → bright white bold (process before *italic* to avoid collision)
    text = _MD_BOLD.sub(_BOLD_REPL, text)
    # *italic* → yellow
    text = _MD_ITALIC.sub(_ITALIC_REPL, text)
    # `code` → magenta
    text = _MD_CODE.sub(_CODE_REPL, text)
    return text

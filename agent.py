#!/usr/bin/env python3
"""
SysControl Agent — Agentic terminal chat interface powered by Ollama.

Spawns the MCP server as a subprocess, converts its tools to the OpenAI format,
then runs a streaming agentic loop so you can ask natural-language questions
about your system and the model will call the right tools autonomously.

Usage:
    uv run agent.py [--provider {cloud,local}] [--model MODEL] [--api-key KEY]
    python agent.py

When selecting the cloud provider you will be prompted to enter your
Ollama API key interactively — no environment variable export needed.
Pass --api-key to skip the prompt entirely (e.g. for scripted/CI use).
"""

import argparse
import getpass
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI

# ── Constants ─────────────────────────────────────────────────────────────────

SERVER_PATH = Path(__file__).parent / "server.py"
PROMPT_PATH = Path(__file__).parent / "prompt.json"
MAX_TOKENS  = 8192
POOL_SIZE   = 4   # max parallel MCP worker processes

# ── Provider config ───────────────────────────────────────────────────────────

CLOUD_MODEL    = "gpt-oss:120b"
CLOUD_BASE_URL = "https://ollama.com/v1"

LOCAL_MODEL    = "qwen2.5"  # any model pulled via: ollama pull <model>
LOCAL_BASE_URL = "http://localhost:11434/v1"
LOCAL_API_KEY  = "ollama"   # Ollama doesn't require a real key

# ANSI colours (degrade gracefully on non-colour terminals)
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
BLUE   = "\033[34m"

# ── MCP Client ────────────────────────────────────────────────────────────────

class MCPClient:
    """Minimal JSON-RPC client that talks to server.py over stdio."""

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

    def _get_or_create(self, index: int) -> MCPClient:
        # Check under lock — if we already have enough clients, return immediately.
        with self._pool_lock:
            if len(self._clients) > index:
                return self._clients[index]
        # Construct the new client OUTSIDE the lock — MCPClient.__init__ spawns
        # a subprocess and runs the MCP handshake, which can take 100–200 ms.
        # Holding the lock for that entire time would block every other thread.
        new_client = MCPClient()
        with self._pool_lock:
            # Another thread may have raced us; only append if still needed.
            while len(self._clients) <= index:
                self._clients.append(new_client)
                return self._clients[index]
        return self._clients[index]

    def call_tools_parallel(
        self, tool_calls: list[dict]
    ) -> list[tuple[str, str, str]]:
        """
        Execute *all* tool calls in `tool_calls` concurrently.

        Returns a list of (tool_call_id, name, result) tuples in the **original
        order** so that messages can be appended deterministically.
        """
        if len(tool_calls) == 1:
            # Fast path: no thread overhead for a single call.
            tc     = tool_calls[0]
            name   = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            result = self._clients[0].call_tool(name, args)
            return [(tc["id"], name, result)]

        n_workers = min(len(tool_calls), self._pool_size)
        results: list[tuple[int, str, str, str]] = []  # (order, id, name, result)

        def _run(order: int, tc: dict) -> tuple[int, str, str, str]:
            client = self._get_or_create(order % n_workers)
            name   = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            result = client.call_tool(name, args)
            return (order, tc["id"], name, result)

        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futures = {ex.submit(_run, i, tc): i for i, tc in enumerate(tool_calls)}
            for fut in as_completed(futures):
                results.append(fut.result())

        results.sort(key=lambda x: x[0])
        return [(tc_id, name, result) for _, tc_id, name, result in results]

    def close_all(self) -> None:
        for client in self._clients:
            client.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_system_prompt() -> str:
    data = json.loads(PROMPT_PATH.read_text())
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


def print_banner() -> None:
    print(f"\n{BOLD}{CYAN}┌─────────────────────────────────────────────────────┐")
    print(f"│               SysControl Agent                      │")
    print(f"│     Your AI system monitoring assistant             │")
    print(f"└─────────────────────────────────────────────────────┘{RESET}")


def print_tool_call(name: str) -> None:
    print(f"\n  {DIM}{YELLOW}⚙  {name}{RESET}", flush=True)


# ── Agentic Loop ──────────────────────────────────────────────────────────────

def run_turn(
    ollama_client:  OpenAI,
    pool:           MCPClientPool,
    tools:          list[dict],
    system_message: dict,          # pre-built {"role": "system", "content": ...}
    messages:       list[dict],
    model:          str,
) -> None:
    """Run one user-turn: stream response, execute any tool calls, repeat."""

    print(f"\n{BOLD}{GREEN}Assistant:{RESET} ", end="", flush=True)

    while True:
        # ── Stream response ────────────────────────────────────────────────
        # system_message is prepended once; messages already contains history.
        stream = ollama_client.chat.completions.create(
            model=model,
            max_tokens=MAX_TOKENS,
            tools=tools,
            messages=[system_message] + messages,
            stream=True,
        )

        # Use a fragment list to avoid O(n²) string copies during streaming.
        content_parts: list[str] = []
        tool_calls:    list[dict] = []  # {id, function: {name, arguments}}
        finish_reason: str | None = None

        for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                continue
            delta = choice.delta

            # Stream text tokens
            if delta.content:
                print(delta.content, end="", flush=True)
                content_parts.append(delta.content)

            # Accumulate streaming tool-call fragments
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    while len(tool_calls) <= tc.index:
                        tool_calls.append({"id": "", "function": {"name": "", "arguments": ""}})
                    entry = tool_calls[tc.index]
                    if tc.id:
                        entry["id"] = tc.id
                    if tc.function and tc.function.name:
                        entry["function"]["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        entry["function"]["arguments"] += tc.function.arguments

            if choice.finish_reason:
                finish_reason = choice.finish_reason

        content = "".join(content_parts)

        # ── Handle finish reason ───────────────────────────────────────────
        # Some models emit finish_reason=None on the last chunk; treat as "stop"
        if finish_reason in ("stop", None) and not tool_calls:
            messages.append({"role": "assistant", "content": content})
            print()   # final newline
            break

        elif finish_reason == "tool_calls":
            # Add the assistant turn with tool-call metadata
            messages.append({
                "role":    "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id":   tc["id"],
                        "type": "function",
                        "function": {
                            "name":      tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        },
                    }
                    for tc in tool_calls
                ],
            })

            # ── Execute tool calls in parallel ─────────────────────────────
            for tc in tool_calls:
                print_tool_call(tc["function"]["name"])

            results = pool.call_tools_parallel(tool_calls)

            for tc_id, _name, result in results:
                messages.append({
                    "role":        "tool",
                    "tool_call_id": tc_id,
                    "content":     result,
                })

            # Continue loop — Ollama will process tool results and respond
            print(f"\n{BOLD}{GREEN}Assistant:{RESET} ", end="", flush=True)

        else:
            # max_tokens, content_filter, etc.
            messages.append({"role": "assistant", "content": content})
            print(f"\n{DIM}[stopped: {finish_reason}]{RESET}")
            break


# ── CLI args ──────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SysControl Agent — AI-powered system monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--provider", choices=["cloud", "local"],
        help="Skip the interactive provider prompt and use this provider directly.",
    )
    parser.add_argument(
        "--model",
        help="Override the default model for the chosen provider.",
    )
    parser.add_argument(
        "--api-key",
        help="Ollama API key for the cloud provider (skips the getpass prompt).",
    )
    return parser.parse_args()


# ── Main REPL ─────────────────────────────────────────────────────────────────

def select_provider(args: argparse.Namespace) -> tuple[str, str, str, str]:
    """
    Return (api_key, base_url, model, label).
    Prefers CLI flags; falls back to interactive prompts.
    """
    # ── Cloud ──────────────────────────────────────────────────────────────
    if args.provider == "cloud":
        api_key = args.api_key or ""
        if not api_key:
            try:
                api_key = getpass.getpass(f"{BOLD}Ollama API key:{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{DIM}Goodbye!{RESET}")
                sys.exit(0)
        if not api_key:
            print(f"{YELLOW}⚠  API key cannot be empty.{RESET}")
            sys.exit(1)
        model = args.model or CLOUD_MODEL
        return api_key, CLOUD_BASE_URL, model, "☁  Cloud"

    # ── Local ──────────────────────────────────────────────────────────────
    if args.provider == "local":
        model = args.model or LOCAL_MODEL
        return LOCAL_API_KEY, LOCAL_BASE_URL, model, "⚙  Local (Ollama)"

    # ── Interactive fallback ───────────────────────────────────────────────
    print(f"\n{BOLD}Select AI model (type {CYAN}cloud{RESET}{BOLD} or {CYAN}local{RESET}{BOLD}):{RESET} ", end="", flush=True)
    while True:
        try:
            choice = input("").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{DIM}Goodbye!{RESET}")
            sys.exit(0)

        if choice == "cloud":
            try:
                api_key = getpass.getpass(f"{BOLD}Ollama API key:{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{DIM}Goodbye!{RESET}")
                sys.exit(0)
            if not api_key:
                print(f"{YELLOW}⚠  API key cannot be empty. Please try again.{RESET}")
                print(f"{BOLD}Select AI model (type {CYAN}cloud{RESET}{BOLD} or {CYAN}local{RESET}{BOLD}):{RESET} ", end="", flush=True)
                continue
            model = args.model or CLOUD_MODEL
            return api_key, CLOUD_BASE_URL, model, "☁  Cloud"

        elif choice == "local":
            model = args.model or LOCAL_MODEL
            return LOCAL_API_KEY, LOCAL_BASE_URL, model, "⚙  Local (Ollama)"

        else:
            print(f"{YELLOW}Please type 'cloud' or 'local':{RESET} ", end="", flush=True)


def main() -> None:
    args = parse_args()
    print_banner()

    if not SERVER_PATH.exists():
        print(f"server.py not found at {SERVER_PATH}", file=sys.stderr)
        sys.exit(1)

    api_key, base_url, model, provider_label = select_provider(args)

    print(f"\n{DIM}Connecting to system monitor backend…{RESET}", end="", flush=True)

    # ── Parallel startup: MCP init + prompt load ───────────────────────────
    mcp_client:    MCPClient | None = None
    system_prompt: str | None       = None
    startup_error: Exception | None = None

    def _start_mcp():
        nonlocal mcp_client, startup_error
        try:
            mcp_client = MCPClient()
        except Exception as exc:
            startup_error = exc

    def _load_prompt():
        nonlocal system_prompt
        system_prompt = load_system_prompt()

    t_mcp    = threading.Thread(target=_start_mcp,    daemon=True)
    t_prompt = threading.Thread(target=_load_prompt,  daemon=True)
    t_mcp.start()
    t_prompt.start()
    t_mcp.join()
    t_prompt.join()

    if startup_error:
        print(f"\nFailed to start MCP server: {startup_error}", file=sys.stderr)
        sys.exit(1)

    pool = MCPClientPool(mcp_client)

    try:
        mcp_tools      = mcp_client.list_tools()
        tools          = mcp_to_openai_tools(mcp_tools)
        system_message = {"role": "system", "content": system_prompt}   # built once
        ollama_client  = OpenAI(api_key=api_key, base_url=base_url)

        print(f"\r{GREEN}✓{RESET} Connected — {len(tools)} tools available. {DIM}[{provider_label}  ·  {model}]{RESET}")
        print(f"{DIM}  Type your question, or 'exit' to quit.{RESET}\n")

        messages: list[dict] = []

        while True:
            try:
                user_input = input(f"{BOLD}{BLUE}You:{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{DIM}Goodbye!{RESET}")
                break

            if not user_input:
                continue

            if user_input.lower() in {"exit", "quit", "bye", ":q"}:
                print(f"{DIM}Goodbye!{RESET}")
                break

            messages.append({"role": "user", "content": user_input})

            try:
                run_turn(ollama_client, pool, tools, system_message, messages, model)
            except Exception as e:
                print(f"\n{YELLOW}Error: {e}{RESET}")

            print()   # blank line between turns

    finally:
        pool.close_all()


if __name__ == "__main__":
    main()

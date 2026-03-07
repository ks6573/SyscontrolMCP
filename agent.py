#!/usr/bin/env python3
"""
SysControl Agent — Agentic terminal chat interface powered by Ollama.

Spawns the MCP server as a subprocess, converts its tools to the OpenAI format,
then runs a streaming agentic loop so you can ask natural-language questions
about your system and the model will call the right tools autonomously.

Usage:
    uv run agent.py
    python agent.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from openai import OpenAI

# ── Constants ─────────────────────────────────────────────────────────────────

SERVER_PATH = Path(__file__).parent / "server.py"
PROMPT_PATH = Path(__file__).parent / "prompt.json"
MAX_TOKENS = 8192

# ── Provider config ───────────────────────────────────────────────────────────

CLOUD_MODEL    = "gpt-oss:120b"
CLOUD_BASE_URL = "https://ollama.com/v1"
CLOUD_API_KEY  = os.environ.get("OLLAMA_API_KEY", "")

LOCAL_MODEL    = "qwen2.5"  # any model pulled via: ollama pull <model>
LOCAL_BASE_URL = "http://localhost:11434/v1"
LOCAL_API_KEY  = "ollama"   # Ollama doesn't require a real key

# ANSI colours (degrade gracefully on non-colour terminals)
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"


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
        self._id = 0
        self._initialize()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send(self, method: str, params: dict | None = None) -> dict:
        msg: dict = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        if not raw:
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"MCP server closed unexpectedly.{(' Server error: ' + err.strip()) if err.strip() else ''}")
        return json.loads(raw)

    def _notify(self, method: str) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _initialize(self) -> None:
        self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "syscontrol-agent", "version": "1.0"},
        })
        self._notify("initialized")

    def list_tools(self) -> list[dict]:
        resp = self._send("tools/list")
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        resp = self._send("tools/call", {"name": name, "arguments": arguments or {}})
        content = resp.get("result", {}).get("content", [])
        return content[0]["text"] if content else str(resp)

    def close(self) -> None:
        try:
            self.proc.terminate()
        except Exception:
            pass


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
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {
                    "type": "object",
                    "properties": {},
                    "required": [],
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
    ollama_client: OpenAI,
    mcp_client: MCPClient,
    tools: list[dict],
    system_prompt: str,
    messages: list[dict],
    model: str,
) -> None:
    """Run one user-turn: stream response, execute any tool calls, repeat."""

    print(f"\n{BOLD}{GREEN}Assistant:{RESET} ", end="", flush=True)

    while True:
        # ── Stream response ────────────────────────────────────────────────
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        stream = ollama_client.chat.completions.create(
            model=model,
            max_tokens=MAX_TOKENS,
            tools=tools,
            messages=full_messages,
            stream=True,
        )

        content = ""
        # Each entry: {"id": str, "function": {"name": str, "arguments": str}}
        tool_calls: list[dict] = []
        finish_reason: str | None = None

        for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                continue
            delta = choice.delta

            # Stream text tokens
            if delta.content:
                print(delta.content, end="", flush=True)
                content += delta.content

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

        # ── Handle finish reason ───────────────────────────────────────────
        # Some models emit finish_reason=None on the last chunk; treat as "stop"
        if finish_reason in ("stop", None) and not tool_calls:
            messages.append({"role": "assistant", "content": content})
            print()  # final newline
            break

        elif finish_reason == "tool_calls":
            # Add the assistant turn with tool-call metadata
            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        },
                    }
                    for tc in tool_calls
                ],
            })

            # Execute each tool and add results
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}
                print_tool_call(name)
                result = mcp_client.call_tool(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

            # Continue loop — Ollama will process tool results and respond
            print(f"\n{BOLD}{GREEN}Assistant:{RESET} ", end="", flush=True)

        else:
            # max_tokens, content_filter, etc.
            messages.append({"role": "assistant", "content": content})
            print(f"\n{DIM}[stopped: {finish_reason}]{RESET}")
            break


# ── Main REPL ─────────────────────────────────────────────────────────────────

def select_provider() -> tuple[str, str, str, str]:
    """Prompt the user to choose between cloud and local and return (api_key, base_url, model, label)."""
    print(f"\n{BOLD}Select AI model (type {CYAN}cloud{RESET}{BOLD} or {CYAN}local{RESET}{BOLD}):{RESET} ", end="", flush=True)
    while True:
        try:
            choice = input("").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{DIM}Goodbye!{RESET}")
            sys.exit(0)
        if choice == "cloud":
            if not CLOUD_API_KEY:
                print(f"{YELLOW}⚠  OLLAMA_API_KEY is not set. Export it and restart.{RESET}")
                sys.exit(1)
            return CLOUD_API_KEY, CLOUD_BASE_URL, CLOUD_MODEL, "☁  Cloud"
        elif choice == "local":
            return LOCAL_API_KEY, LOCAL_BASE_URL, LOCAL_MODEL, "⚙  Local (Ollama)"
        else:
            print(f"{YELLOW}Please type 'cloud' or 'local':{RESET} ", end="", flush=True)


def main() -> None:
    print_banner()

    api_key, base_url, model, provider_label = select_provider()

    if not SERVER_PATH.exists():
        print(f"server.py not found at {SERVER_PATH}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{DIM}Connecting to system monitor backend…{RESET}", end="", flush=True)
    try:
        mcp_client = MCPClient()
    except Exception as e:
        print(f"\nFailed to start MCP server: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        mcp_tools = mcp_client.list_tools()
        tools = mcp_to_openai_tools(mcp_tools)
        system_prompt = load_system_prompt()
        ollama_client = OpenAI(api_key=api_key, base_url=base_url)

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
                run_turn(ollama_client, mcp_client, tools, system_prompt, messages, model)
            except Exception as e:
                print(f"\n{YELLOW}Error: {e}{RESET}")

            print()  # blank line between turns

    finally:
        mcp_client.close()


if __name__ == "__main__":
    main()

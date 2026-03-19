#!/usr/bin/env python3
"""
SysControl Agent — Interactive CLI.

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
import datetime
import getpass
import itertools
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import fcntl as _fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

import openai
from openai import OpenAI

from agent.core import (
    BLUE, BOLD, CLOUD_BASE_URL, CLOUD_MODEL, CYAN, DIM, GREEN,
    LOCAL_API_KEY, LOCAL_BASE_URL, LOCAL_MODEL, MAX_TOKENS,
    RESET, SERVER_PATH, YELLOW,
    MCPClient, MCPClientPool,
    _colorize, load_system_prompt, mcp_to_openai_tools,
)

# ── Memory ────────────────────────────────────────────────────────────────────

MEMORY_FILE = Path(__file__).parent.parent / "SysControl_Memory.md"

# Phrases that signal the user wants to end the session
EXIT_PHRASES: frozenset[str] = frozenset({
    "exit", "quit", "bye", "goodbye", "good bye", "farewell",
    "see ya", "see you", "cya", "later", "take care", "peace",
    "done", "close", "end", "stop", ":q", "q", "adios", "adieu",
    "ttyl", "ttfn", "night", "goodnight", "good night",
})

_PRIVACY_NOTICE = (
    f"\n{DIM}╔══════════════════════════════════════════════════════════════╗\n"
    f"║  Privacy Notice                                              ║\n"
    f"║  SysControl stores only what you explicitly choose to save.  ║\n"
    f"║  No personal data is retained by the agent or the LLM.       ║\n"
    f"║  Ollama processes queries locally — see ollama.com/tos for   ║\n"
    f"║  full details on cloud usage (if applicable).                ║\n"
    f"╚══════════════════════════════════════════════════════════════╝{RESET}\n"
)


def load_memory() -> str | None:
    """Return the contents of SysControl_Memory.md if it exists, else None."""
    if MEMORY_FILE.exists():
        text = MEMORY_FILE.read_text(encoding="utf-8").strip()
        return text if text else None
    return None


def _format_conversation(messages: list[dict]) -> str:
    """Render the message list as a human-readable markdown block."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content") or ""
        if role == "system" or not content:
            continue
        if role == "user":
            lines.append(f"**You:** {content}")
        elif role == "assistant":
            lines.append(f"**Assistant:** {content}")
        # Skip tool messages — they're internal plumbing
    return "\n\n".join(lines)


def offer_memory_save(messages: list[dict]) -> None:
    """
    Ask the user whether to append this session to SysControl_Memory.md.
    Called just before the agent exits.
    """
    # Only bother if there's actual conversation to save
    has_content = any(
        m.get("role") in ("user", "assistant") and m.get("content")
        for m in messages
    )
    if not has_content:
        return

    print(_PRIVACY_NOTICE)
    print(f"{BOLD}Would you like to save this session to memory for future reference?{RESET}")
    print(f"{DIM}  Memory is appended to SysControl_Memory.md (never overwritten).{RESET}")
    print(f"{DIM}  Type 'yes'/'no', or choose format: 'md' / 'txt'{RESET}")
    print(f"{BOLD}Save session? [yes/no/md/txt]:{RESET} ", end="", flush=True)

    try:
        answer = input("").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if answer in ("yes", "y", "md", "markdown"):
        _append_memory(messages, fmt="md")
    elif answer in ("txt", "text"):
        _append_memory(messages, fmt="txt")
    else:
        print(f"{DIM}Session not saved.{RESET}")


def _append_memory(messages: list[dict], fmt: str = "md") -> None:
    """Append the current session to SysControl_Memory.md."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    body = _format_conversation(messages)

    if fmt == "md":
        separator = f"\n\n---\n\n## Session — {timestamp}\n\n{body}\n"
    else:
        separator = f"\n\n{'='*60}\nSession — {timestamp}\n{'='*60}\n\n"
        # Strip markdown bold markers for plain text
        separator += body.replace("**You:**", "You:").replace("**Assistant:**", "Assistant:")
        separator += "\n"

    # Open for append (creates if missing); use an exclusive file lock so
    # concurrent CLI sessions don't interleave their writes.
    with MEMORY_FILE.open("a", encoding="utf-8") as fh:
        if _HAS_FCNTL:
            _fcntl.flock(fh, _fcntl.LOCK_EX)
        try:
            # Seek to the true end of file *after* acquiring the lock so that
            # tell() reflects any bytes written by a concurrent session between
            # our open() and flock() calls.
            fh.seek(0, 2)
            if fh.tell() == 0:
                fh.write("# SysControl Memory\n\nThis file is appended automatically. Edit freely.\n")
            fh.write(separator)
        finally:
            if _HAS_FCNTL:
                _fcntl.flock(fh, _fcntl.LOCK_UN)

    print(f"{GREEN}✓ Session appended to {MEMORY_FILE.name}{RESET}")


# ── Banner ────────────────────────────────────────────────────────────────────

def print_banner() -> None:
    print(f"\n{BOLD}{CYAN}┌─────────────────────────────────────────────────────┐")
    print(f"│               SysControl Agent                      │")
    print(f"│     Your AI system monitoring assistant             │")
    print(f"└─────────────────────────────────────────────────────┘{RESET}")
    memory = load_memory()
    if memory:
        print(f"{DIM}  Memory file found — previous context will be included.{RESET}")


# ── Error classification ───────────────────────────────────────────────────────

class _LLMError(Exception):
    """Wraps errors from the OpenAI/Ollama API call."""

class _ToolError(Exception):
    """Wraps errors from MCP tool execution."""

class _MCPError(Exception):
    """Wraps errors from the MCP subprocess itself (crash or closed pipe)."""


# ── Spinner ────────────────────────────────────────────────────────────────────

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class _Spinner:
    """Thread-backed terminal spinner — no-ops when stdout is not a TTY."""

    def __init__(self) -> None:
        self._message = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_tty: bool = sys.stdout.isatty()

    def start(self, message: str = "") -> None:
        self.stop()   # stop any currently running spinner first
        if not self._is_tty:
            return
        self._message = message
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._stop.set()
            self._thread.join(timeout=0.5)
        if self._is_tty:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def _run(self) -> None:
        for frame in itertools.cycle(_SPINNER_FRAMES):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r{DIM}{frame}  {self._message}{RESET}")
            sys.stdout.flush()
            time.sleep(0.08)
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


# ── History management ────────────────────────────────────────────────────────

MAX_HISTORY_MESSAGES = 40  # ~20 user turns; keeps context well within model limits


def _prune_history(messages: list[dict], max_messages: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    """
    Trim message history to at most max_messages entries while preserving
    tool-call coherence: never separate an assistant tool_calls message from
    the tool result messages that follow it.

    Groups the history into user-anchored turn chunks, then drops the oldest
    chunks until the total fits within the budget.
    """
    if len(messages) <= max_messages:
        return messages

    groups: list[list[dict]] = []
    current: list[dict] = []
    for msg in messages:
        if msg["role"] == "user" and current:
            groups.append(current)
            current = []
        current.append(msg)
    if current:
        groups.append(current)

    total = sum(len(g) for g in groups)
    while groups and total > max_messages:
        total -= len(groups[0])
        groups.pop(0)

    return [msg for group in groups for msg in group]


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

    start_time = time.monotonic()
    spinner = _Spinner()

    while True:
        # Trim history to prevent context-window overflow on long sessions.
        messages[:] = _prune_history(messages)

        # ── Stream response ────────────────────────────────────────────────
        # system_message is prepended once; messages already contains history.
        spinner.start("Thinking…")
        try:
            stream = ollama_client.chat.completions.create(
                model=model,
                max_tokens=MAX_TOKENS,
                tools=tools,
                messages=[system_message] + messages,
                stream=True,
            )
        except openai.APITimeoutError as exc:
            spinner.stop()
            raise _LLMError(f"LLM request timed out ({exc})") from exc
        except openai.APIConnectionError as exc:
            spinner.stop()
            raise _LLMError(f"Cannot reach LLM endpoint: {exc}") from exc
        except openai.AuthenticationError as exc:
            spinner.stop()
            raise _LLMError(f"Invalid API key: {exc}") from exc
        except openai.APIStatusError as exc:
            spinner.stop()
            raise _LLMError(f"LLM API error {exc.status_code}: {exc.message}") from exc
        except openai.OpenAIError as exc:
            spinner.stop()
            raise _LLMError(f"LLM error: {exc}") from exc

        # Use a fragment list to avoid O(n²) string copies during streaming.
        content_parts: list[str] = []
        tool_calls:    list[dict] = []  # {id, function: {name, arguments}}
        finish_reason: str | None = None
        _first_content = True   # used to defer "Assistant: " header until first token

        # Line buffer: accumulate partial lines so colorization applies to
        # complete lines (markers rarely split across a line boundary).
        _pending = ""

        for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                continue
            delta = choice.delta

            # Stream text tokens — buffer by line, colorize on newline.
            if delta.content:
                if _first_content:
                    spinner.stop()
                    sys.stdout.write(f"\n{BOLD}{GREEN}Assistant:{RESET} ")
                    sys.stdout.flush()
                    _first_content = False
                content_parts.append(delta.content)
                _pending += delta.content
                while "\n" in _pending:
                    line, _pending = _pending.split("\n", 1)
                    print(_colorize(line), flush=True)

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

        # Flush any partial last line (no trailing newline from model).
        if _pending:
            print(_colorize(_pending), end="", flush=True)
            _pending = ""

        # Stop spinner in case the model produced only tool calls (no text).
        if _first_content:
            spinner.stop()
            _first_content = False

        content = "".join(content_parts)

        # ── Handle finish reason ───────────────────────────────────────────
        # Some models emit finish_reason=None on the last chunk; treat as "stop"
        if finish_reason in ("stop", None) and not tool_calls:
            messages.append({"role": "assistant", "content": content})
            print()   # final newline
            elapsed = time.monotonic() - start_time
            print(f"{DIM}  thought for {elapsed:.1f}s{RESET}")
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
            n = len(tool_calls)
            label = tool_calls[0]["function"]["name"] + (f" +{n - 1} more" if n > 1 else "")
            spinner.start(f"Running {label}…")
            try:
                results = pool.call_tools_parallel(tool_calls)
            except RuntimeError as exc:
                spinner.stop()
                raise _MCPError(f"MCP server crashed or closed: {exc}") from exc
            except Exception as exc:
                spinner.stop()
                raise _ToolError(f"Tool execution failed: {exc}") from exc
            spinner.stop()

            for tc_id, _name, result in results:
                messages.append({
                    "role":        "tool",
                    "tool_call_id": tc_id,
                    "content":     result,
                })

            # Continue loop — next iteration's spinner + first-content guard
            # will handle the "Assistant: " header.

        else:
            # max_tokens, content_filter, etc.
            messages.append({"role": "assistant", "content": content})
            elapsed = time.monotonic() - start_time
            print(f"\n{DIM}[stopped: {finish_reason}] thought for {elapsed:.1f}s{RESET}")
            break


# ── Ollama model detection ────────────────────────────────────────────────────

def _fetch_ollama_models(base_url: str = "http://localhost:11434") -> list[str]:
    """Return sorted list of locally installed Ollama model names.
    Returns an empty list if Ollama is not running or unreachable (3 s timeout).
    """
    try:
        req = urllib.request.Request(
            f"{base_url}/api/tags",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read().decode())
        return sorted(m["name"] for m in data.get("models", []))
    except Exception:
        return []


def _pick_model(models: list[str]) -> str:
    """Present a numbered list of models and return the user's choice."""
    print(f"\n{BOLD}Available local models:{RESET}")
    for i, name in enumerate(models, 1):
        print(f"  {CYAN}{i}{RESET}) {name}")
    print(f"{BOLD}Select model [1-{len(models)}]:{RESET} ", end="", flush=True)
    while True:
        try:
            raw = input("").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{DIM}Goodbye!{RESET}")
            sys.exit(0)
        if raw.isdigit() and 1 <= int(raw) <= len(models):
            return models[int(raw) - 1]
        if raw in models:
            return raw
        print(f"{YELLOW}Please enter a number between 1 and {len(models)}:{RESET} ", end="", flush=True)


def _resolve_local_model() -> str:
    """Detect installed Ollama models and return the user's selection.

    - 0 models / unreachable → warns and returns LOCAL_MODEL fallback
    - 1 model               → auto-selects it silently
    - 2+ models             → shows numbered picker
    """
    models = _fetch_ollama_models()
    if not models:
        print(f"{YELLOW}⚠  No local models detected (is Ollama running?). "
              f"Using default: {LOCAL_MODEL}{RESET}")
        return LOCAL_MODEL
    if len(models) == 1:
        print(f"{DIM}  Auto-selected the only installed model: {models[0]}{RESET}")
        return models[0]
    return _pick_model(models)


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
        model = args.model or _resolve_local_model()
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
            model = args.model or _resolve_local_model()
            return LOCAL_API_KEY, LOCAL_BASE_URL, model, "⚙  Local (Ollama)"

        else:
            print(f"{YELLOW}Please type 'cloud' or 'local':{RESET} ", end="", flush=True)


# ── Main REPL ─────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    print_banner()

    if not SERVER_PATH.exists():
        print(f"mcp/server.py not found at {SERVER_PATH}", file=sys.stderr)
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
        mcp_tools = mcp_client.list_tools()
        tools     = mcp_to_openai_tools(mcp_tools)

        # Inject available tool names so the model can answer introspection questions
        tool_names = [t["function"]["name"] for t in tools]
        tool_list_block = (
            "\n\n---\n\n# Available Tools\n\n"
            "You have access to the following tools (call them by name):\n"
            + "\n".join(f"- {n}" for n in tool_names)
        )

        # Inject saved memory into the system prompt so the agent has prior context
        memory = load_memory()
        full_system = system_prompt + tool_list_block
        if memory:
            full_system += (
                "\n\n---\n\n# Saved Memory (from previous sessions)\n\n"
                + memory
            )

        system_message = {"role": "system", "content": full_system}   # built once
        ollama_client  = OpenAI(api_key=api_key, base_url=base_url, timeout=120.0)

        print(f"\r{GREEN}✓{RESET} Connected — {len(tools)} tools available. {DIM}[{provider_label}  ·  {model}]{RESET}")
        print(f"{DIM}  Type your question, or 'exit' / 'bye' / 'goodbye' to quit.{RESET}\n")

        messages: list[dict] = []

        while True:
            try:
                user_input = input(f"{BOLD}{BLUE}You:{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{DIM}Goodbye!{RESET}")
                offer_memory_save(messages)
                break

            if not user_input:
                continue

            if user_input.lower() in EXIT_PHRASES:
                print(f"{DIM}Goodbye!{RESET}")
                offer_memory_save(messages)
                break

            messages.append({"role": "user", "content": user_input})

            try:
                run_turn(ollama_client, pool, tools, system_message, messages, model)
            except _LLMError as e:
                print(f"\n{YELLOW}LLM error: {e}{RESET}")
                print(f"{DIM}  Check your API key or network connection, then try again.{RESET}")
            except _MCPError as e:
                print(f"\n{YELLOW}MCP server error: {e}{RESET}")
                print(f"{DIM}  The system monitor backend crashed — restarting is recommended.{RESET}")
                break
            except _ToolError as e:
                print(f"\n{YELLOW}Tool error: {e}{RESET}")
                print(f"{DIM}  The tool failed but the session is intact — try again.{RESET}")
            except Exception as e:
                print(f"\n{YELLOW}Unexpected error: {e}{RESET}")

            print()   # blank line between turns

    finally:
        pool.close_all()


if __name__ == "__main__":
    main()

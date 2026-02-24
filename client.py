#!/usr/bin/env python3
"""
MCP Client: System Monitor Test Client
Launches server.py as a subprocess and exercises each tool interactively.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

SERVER_PATH = Path(__file__).parent / "server.py"


class MCPClient:
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

    def _next_id(self):
        self._id += 1
        return self._id

    def _send(self, method: str, params: dict = None) -> dict:
        msg = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        return json.loads(raw)

    def _notify(self, method: str):
        msg = {"jsonrpc": "2.0", "method": method}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _initialize(self):
        self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"}
        })
        self._notify("initialized")

    def list_tools(self) -> list:
        resp = self._send("tools/list")
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict = None) -> str:
        resp = self._send("tools/call", {"name": name, "arguments": arguments or {}})
        content = resp.get("result", {}).get("content", [])
        return content[0]["text"] if content else str(resp)

    def close(self):
        self.proc.terminate()


def pretty_section(title: str, data: str):
    width = 60
    print("\n" + "═" * width)
    print(f"  {title}")
    print("═" * width)
    try:
        parsed = json.loads(data)
        print(json.dumps(parsed, indent=2))
    except Exception:
        print(data)


def interactive_menu(client: MCPClient):
    tools = client.list_tools()
    tool_names = [t["name"] for t in tools]

    menu = textwrap.dedent("""
    ┌─────────────────────────────────────────┐
    │        System Monitor MCP Client        │
    └─────────────────────────────────────────┘
    Available tools:
    """)
    for i, t in enumerate(tools, 1):
        menu += f"  {i}. {t['name']:25s}  {t['description'][:45]}\n"
    menu += "  0. Exit\n"

    while True:
        print(menu)
        choice = input("Select a tool (number): ").strip()
        if choice == "0":
            print("Goodbye!")
            break
        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(tools):
                raise ValueError
        except ValueError:
            print("Invalid choice, try again.")
            continue

        tool = tools[idx]
        args = {}

        # Prompt for arguments if the tool has any
        props = tool.get("inputSchema", {}).get("properties", {})
        for prop_name, prop_info in props.items():
            default = prop_info.get("default", "")
            val = input(f"  {prop_name} [{default}]: ").strip()
            if val:
                # Cast to correct type
                if prop_info.get("type") == "integer":
                    args[prop_name] = int(val)
                else:
                    args[prop_name] = val
            elif default != "":
                args[prop_name] = default

        result = client.call_tool(tool["name"], args)
        pretty_section(tool["name"].upper(), result)
        input("\nPress Enter to continue...")


def quick_snapshot(client: MCPClient):
    """Run a quick full snapshot and print it."""
    print("\n⏳ Fetching full system snapshot...\n")
    result = client.call_tool("get_full_snapshot")
    pretty_section("FULL SYSTEM SNAPSHOT", result)


def main():
    print("Starting MCP System Monitor Client...")
    client = MCPClient()

    if len(sys.argv) > 1 and sys.argv[1] == "--snapshot":
        quick_snapshot(client)
    else:
        interactive_menu(client)

    client.close()


if __name__ == "__main__":
    main()
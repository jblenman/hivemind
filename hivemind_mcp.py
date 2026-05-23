"""
hivemind_mcp.py — MCP server exposing Hivemind's six tools.

Wraps the existing `execute_tool` dispatcher from hivemind-code.py and serves
the same six tools (read_file, write_file, edit_file, list_files, search_files,
run_command) over the Model Context Protocol via stdio.

Trust mode is forced on for MCP usage because the interactive confirmation flow
(`[y]es / [n]o / [v]iew diff`) cannot run over stdio. If you don't want a
particular client to have write/edit/run-command access, don't register this
server with it.

Project directory (where the tools operate) is the current working directory
at server startup. Run this from inside the directory you want the agent to
act in.

## Register with Claude Desktop

Add to `claude_desktop_config.json`:

    {
      "mcpServers": {
        "hivemind": {
          "command": "python",
          "args": ["C:/absolute/path/to/hivemind_mcp.py"]
        }
      }
    }

Restart Claude Desktop. The six tools will appear under the MCP tool list.

## Requirements

    pip install mcp

That's the only dependency — everything else is hivemind's own stdlib code.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions


# ── Load hivemind-code.py (hyphenated filename — not a valid module name) ──
_HERE = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "hivemind_code",
    _HERE / "hivemind-code.py",
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Could not locate hivemind-code.py next to hivemind_mcp.py")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["hivemind_code"] = _MODULE
_SPEC.loader.exec_module(_MODULE)

execute_tool = _MODULE.execute_tool
TOOLS = _MODULE.TOOLS


# ── Server setup ────────────────────────────────────────────────────────────
PROJECT_DIR = os.getcwd()
server: Server = Server("hivemind")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Translate hivemind's Ollama-flavored TOOLS list into MCP Tool descriptors."""
    return [
        types.Tool(
            name=t["function"]["name"],
            description=t["function"]["description"],
            inputSchema=t["function"]["parameters"],
        )
        for t in TOOLS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Route an MCP tool call to hivemind's execute_tool dispatcher.

    trust_mode is forced True because confirmation prompts cannot be answered
    over stdio. Path containment, the dangerous-command blocklist, and the
    other safety rails inside execute_tool still apply.
    """
    result = execute_tool(name, arguments, PROJECT_DIR, trust_mode=True)
    return [types.TextContent(type="text", text=str(result))]


# ── Entry point ────────────────────────────────────────────────────────────
async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="hivemind",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())

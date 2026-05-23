# MCP Wrapper for Hivemind — Implementation Spec

## Goal

Expose Hivemind's six existing tools (`read_file`, `write_file`, `edit_file`, `list_files`, `search_files`, `run_command`) as an MCP server over stdio, so any MCP-aware client (Claude Desktop, Claude Code, etc.) can call them.

## Output

Create exactly one new file: **`hivemind_mcp.py`** at the repo root. Do not modify any existing files.

## Approach (read this carefully)

1. Use the official `mcp` Python SDK from PyPI (`pip install mcp`). Use the **low-level `Server` class** under `mcp.server`. Do NOT use `FastMCP`.
2. Import the existing **`execute_tool`** function from `hivemind-code.py` — do NOT reimplement the tool logic. The wrapper just routes MCP `call_tool` requests to `execute_tool(name, args, project_dir, trust_mode=True)`.
3. Reuse the **`TOOLS`** list (already defined in `hivemind-code.py`) as the source of tool schemas. Each entry has the shape `{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}` — convert each to an `mcp.types.Tool(name=..., description=..., inputSchema=...)`.
4. Use stdio transport (`mcp.server.stdio.stdio_server`). The script's `main()` should be an `async def` that creates the server and runs it on stdio.
5. `project_dir` is the current working directory at server startup — capture it once at module load via `os.getcwd()`.
6. Trust mode is forced **on** for MCP usage (no interactive confirmations possible over stdio). Document this in the file's module docstring.

## Required imports

```python
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
```

## Importing from hivemind-code.py

The file is named `hivemind-code.py` (with a hyphen), which is not a valid Python module name for `import hivemind-code`. Use `importlib` to load it:

```python
_HERE = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "hivemind_code",
    _HERE / "hivemind-code.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["hivemind_code"] = _MODULE
_SPEC.loader.exec_module(_MODULE)

execute_tool = _MODULE.execute_tool
TOOLS = _MODULE.TOOLS
```

## Tool schema conversion

Each entry in `TOOLS` has this shape:

```python
{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "...",
        "parameters": {
            "type": "object",
            "properties": {...},
            "required": [...]
        }
    }
}
```

Convert each one to:

```python
types.Tool(
    name=t["function"]["name"],
    description=t["function"]["description"],
    inputSchema=t["function"]["parameters"],
)
```

## Server skeleton

```python
PROJECT_DIR = os.getcwd()
server = Server("hivemind")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
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
    result = execute_tool(name, arguments, PROJECT_DIR, trust_mode=True)
    return [types.TextContent(type="text", text=str(result))]


async def main():
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
```

## Module docstring

At the top of the file, write a docstring explaining:
- What this is (MCP server exposing Hivemind's filesystem and shell tools)
- Transport (stdio)
- That trust_mode is forced on (no interactive confirmations available over MCP)
- How to register with Claude Desktop (point at the `claude_desktop_config.json` snippet — see Usage section below)

## Usage section (include as a comment block near the top, or as a separate `USAGE` constant)

```
To use with Claude Desktop, add this to claude_desktop_config.json:

  {
    "mcpServers": {
      "hivemind": {
        "command": "python",
        "args": ["C:/absolute/path/to/hivemind_mcp.py"]
      }
    }
  }

Restart Claude Desktop. The six hivemind tools will appear as available MCP tools.
```

## Acceptance criteria

- File compiles: `python -c "import ast; ast.parse(open('hivemind_mcp.py').read())"` succeeds.
- File imports without error: `python -c "import importlib.util, sys; spec = importlib.util.spec_from_file_location('m', 'hivemind_mcp.py'); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)"` succeeds (assuming `mcp` is installed).
- `list_tools` returns 6 tools.
- `call_tool("list_files", {"pattern": "*.py"})` returns a `list[TextContent]` with a non-empty text containing at least one filename.

## Things to NOT do

- Do NOT modify `hivemind-code.py` or `hivemind.py`.
- Do NOT add pip dependencies beyond `mcp`.
- Do NOT add features beyond exposing the six tools.
- Do NOT change the trust-mode behavior (over stdio, confirmations aren't possible — trust is the only sensible choice).

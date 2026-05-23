#!/usr/bin/env python
"""
hivemind-code.py - Local AI Coding Assistant

A coding assistant powered by local LLMs via Ollama. Routes to the best
available model across your fleet using hivemind.py as the routing engine.
Supports file reading/writing, code editing, command execution, and
multi-turn agentic tool loops.

Usage:
    python hivemind-code.py                              # interactive mode
    python hivemind-code.py "add error handling"         # start with prompt
    python hivemind-code.py -r quality "refactor auth"   # force quality route
    python hivemind-code.py --resume                     # resume session
    python hivemind-code.py --trust "run the tests"      # auto-approve actions
    python hivemind-code.py --one-shot "explain this"    # respond and exit

Requires: Ollama running on configured machines. Python 3.8+, no deps.
Config: hivemind-config.json (same directory as this script)
"""

import sys
import os
import json
import argparse
import glob as glob_module
import re
import subprocess
import time
import socket
import urllib.request
import urllib.error
import datetime
import difflib

# Fix Windows CP1252 encoding
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Add script directory to path for hivemind import
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from hivemind import (
    load_config, check_machine, classify, find_target,
    find_model_anywhere, show_status
)

# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "hivemind-config.json")
MAX_DEPTH = 20
MAX_FILE_READ = 50000       # chars
MAX_CMD_OUTPUT = 10000      # chars
CMD_TIMEOUT = 120           # seconds
SESSION_MAX_AGE = 86400     # 24 hours

# Route hierarchy for escalation (higher = more capable)
ROUTE_RANK = {"quick": 0, "interactive": 1, "quality": 2}

# Preferred models for local fallback (priority order)
MODEL_PREFERENCE = [
    "gpt-oss:20b", "phi4:14b", "qwen3-coder:30b",
    "qwen2.5-coder:7b", "qwen2.5-coder:3b",
]

# Model name prefixes known to support tool calling
TOOL_CAPABLE_PREFIXES = [
    "gpt-oss", "qwen2.5-coder", "qwen3-coder", "devstral",
    "phi4", "nemotron", "qwen3",
]

# Dangerous command patterns (regex)
DANGEROUS_PATTERNS = [
    r"rm\s+-r[fe]*\s+/\s*$",
    r"rm\s+-r[fe]*\s+/[a-zA-Z]",
    r"format\s+[A-Za-z]:",
    r"del\s+/[sS].*[A-Za-z]:\\$",
    r"del\s+/[sS]\s+/[qQ]\s+[A-Za-z]:\\",
    r"mkfs\.",
    r"dd\s+if=.*\s+of=/dev/[sh]d",
    r">\s*/dev/sd",
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;:",  # fork bomb
]

VERSION = "0.1.0"

# ── Safety Utilities ─────────────────────────────────────────────────────────

def path_in_project(path, project_dir):
    """Check if a resolved path is within the project directory."""
    real_path = os.path.realpath(os.path.abspath(path))
    real_project = os.path.realpath(os.path.abspath(project_dir))
    if sys.platform == 'win32':
        real_path = real_path.lower()
        real_project = real_project.lower()
    return real_path.startswith(real_project + os.sep) or real_path == real_project


def resolve_path(path, project_dir):
    """Resolve a path relative to project dir. Returns absolute path or None if outside."""
    if os.path.isabs(path):
        full = path
    else:
        full = os.path.join(project_dir, path)
    full = os.path.realpath(os.path.abspath(full))
    if not path_in_project(full, project_dir):
        return None
    return full


def is_binary(filepath):
    """Check if a file is binary by looking for null bytes in first 1024 bytes."""
    try:
        with open(filepath, 'rb') as f:
            chunk = f.read(1024)
            return b'\x00' in chunk
    except (IOError, OSError):
        return False


def is_dangerous_command(cmd):
    """Check if a command matches any dangerous pattern."""
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd):
            return True
    return False


# ── User Confirmation ────────────────────────────────────────────────────────

def confirm_action(description, trust_mode=False):
    """Ask user to confirm an action. Returns True if approved."""
    if trust_mode:
        print("  [auto-approved] {}".format(description))
        return True
    print("\n  {}".format(description))
    try:
        choice = input("  Approve? [y]es / [n]o: ").strip().lower()
        return choice in ('y', 'yes')
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def confirm_write(description, old_content=None, new_content=None, trust_mode=False):
    """Ask user to confirm a file write, with optional diff view."""
    if trust_mode:
        print("  [auto-approved] {}".format(description))
        return True
    print("\n  {}".format(description))
    while True:
        try:
            if old_content is not None:
                choice = input("  Approve? [y]es / [n]o / [v]iew diff: ").strip().lower()
            else:
                choice = input("  Approve? [y]es / [n]o / [v]iew: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if choice in ('y', 'yes'):
            return True
        if choice in ('n', 'no'):
            return False
        if choice in ('v', 'view'):
            if old_content is not None and new_content is not None:
                diff = difflib.unified_diff(
                    old_content.splitlines(keepends=True),
                    new_content.splitlines(keepends=True),
                    fromfile='current',
                    tofile='proposed',
                )
                diff_text = ''.join(diff)
                if diff_text:
                    print(diff_text)
                else:
                    print("  (no differences)")
            elif new_content is not None:
                print(new_content[:3000])
                if len(new_content) > 3000:
                    print("  ... ({} more chars)".format(len(new_content) - 3000))


# ── Tool Implementations ─────────────────────────────────────────────────────

def tool_read_file(args, project_dir):
    """Read file contents with numbered lines."""
    path = args.get("path", "")
    full = resolve_path(path, project_dir)
    if full is None:
        return "Error: Path '{}' is outside the project directory.".format(path)
    if not os.path.exists(full):
        return "Error: File '{}' does not exist.".format(path)
    if not os.path.isfile(full):
        return "Error: '{}' is not a file.".format(path)
    if is_binary(full):
        return "Error: '{}' appears to be a binary file.".format(path)

    try:
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except (IOError, OSError) as e:
        return "Error reading '{}': {}".format(path, e)

    offset = args.get("offset", 1)
    if not isinstance(offset, int) or offset < 1:
        offset = 1
    limit = args.get("limit")
    if not isinstance(limit, int) or limit < 1:
        limit = len(lines)

    selected = lines[offset - 1:offset - 1 + limit]
    result_lines = ["Contents of {} ({} lines):".format(path, len(lines))]
    for i, line in enumerate(selected, start=offset):
        result_lines.append("{:>4}\t{}".format(i, line.rstrip('\n\r')))

    result = '\n'.join(result_lines)
    if len(result) > MAX_FILE_READ:
        result = result[:MAX_FILE_READ] + "\n... (truncated at {} chars)".format(MAX_FILE_READ)
    return result


def tool_write_file(args, project_dir, trust_mode=False):
    """Write/create a file."""
    path = args.get("path", "")
    content = args.get("content", "")
    full = resolve_path(path, project_dir)
    if full is None:
        return "Error: Path '{}' is outside the project directory.".format(path)

    # Read existing content for diff
    old_content = None
    if os.path.exists(full):
        try:
            with open(full, 'r', encoding='utf-8', errors='replace') as f:
                old_content = f.read()
        except (IOError, OSError):
            pass

    if old_content is not None:
        desc = "Overwrite file: {} ({} -> {} chars)".format(
            path, len(old_content), len(content))
    else:
        desc = "Create file: {} ({} chars)".format(path, len(content))

    if not confirm_write(desc, old_content=old_content, new_content=content,
                         trust_mode=trust_mode):
        return "Cancelled: File write was not approved by user."

    parent = os.path.dirname(full)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    try:
        with open(full, 'w', encoding='utf-8', newline='') as f:
            f.write(content)
        return "Successfully wrote {} ({} chars).".format(path, len(content))
    except (IOError, OSError) as e:
        return "Error writing '{}': {}".format(path, e)


def tool_edit_file(args, project_dir, trust_mode=False):
    """Find-and-replace edit in a file."""
    path = args.get("path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")

    full = resolve_path(path, project_dir)
    if full is None:
        return "Error: Path '{}' is outside the project directory.".format(path)
    if not os.path.exists(full):
        return "Error: File '{}' does not exist.".format(path)

    try:
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except (IOError, OSError) as e:
        return "Error reading '{}': {}".format(path, e)

    count = content.count(old_string)
    if count == 0:
        return "Error: old_string not found in '{}'.".format(path)
    if count > 1:
        return ("Error: old_string found {} times in '{}'. "
                "Must be unique — provide more surrounding context.").format(count, path)

    new_content = content.replace(old_string, new_string, 1)

    desc = "Edit file: {} (replace {} chars with {} chars)".format(
        path, len(old_string), len(new_string))
    if not confirm_write(desc, old_content=content, new_content=new_content,
                         trust_mode=trust_mode):
        return "Cancelled: Edit was not approved by user."

    try:
        with open(full, 'w', encoding='utf-8', newline='') as f:
            f.write(new_content)
        return "Successfully edited {}.".format(path)
    except (IOError, OSError) as e:
        return "Error writing '{}': {}".format(path, e)


def tool_list_files(args, project_dir):
    """List files matching a glob pattern."""
    pattern = args.get("pattern", "*")
    path = args.get("path", "")

    if path:
        base = resolve_path(path, project_dir)
        if base is None:
            return "Error: Path '{}' is outside the project directory.".format(path)
    else:
        base = project_dir

    search = os.path.join(base, pattern)
    try:
        matches = glob_module.glob(search, recursive=True)
    except Exception as e:
        return "Error in glob: {}".format(e)

    results = []
    for m in sorted(matches):
        if os.path.isfile(m) and path_in_project(m, project_dir):
            try:
                rel = os.path.relpath(m, project_dir)
                results.append(rel.replace('\\', '/'))
            except ValueError:
                results.append(m.replace('\\', '/'))

    if not results:
        return "No files matching '{}' found.".format(pattern)

    if len(results) > 200:
        return '\n'.join(results[:200]) + "\n... ({} total files)".format(len(results))
    return '\n'.join(results)


def tool_search_files(args, project_dir):
    """Search file contents with regex."""
    pattern = args.get("pattern", "")
    path = args.get("path", "")
    glob_filter = args.get("glob_filter", "")

    if path:
        base = resolve_path(path, project_dir)
        if base is None:
            return "Error: Path '{}' is outside the project directory.".format(path)
    else:
        base = project_dir

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return "Error: Invalid regex pattern: {}".format(e)

    # Find files to search
    if glob_filter:
        file_pattern = os.path.join(base, "**", glob_filter)
        files = glob_module.glob(file_pattern, recursive=True)
    else:
        files = []
        skip_dirs = {'.git', 'node_modules', '__pycache__', 'venv', '.venv',
                     'dist', 'build', '.hivemind', '.next', 'target'}
        for root, dirs, filenames in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in skip_dirs]
            for fname in filenames:
                files.append(os.path.join(root, fname))

    results = []
    for fpath in sorted(files):
        if not os.path.isfile(fpath):
            continue
        if not path_in_project(fpath, project_dir):
            continue
        if is_binary(fpath):
            continue
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                for lineno, line in enumerate(f, 1):
                    if regex.search(line):
                        rel = os.path.relpath(fpath, project_dir).replace('\\', '/')
                        results.append("{}:{}:{}".format(rel, lineno, line.rstrip()))
                        if len(results) >= 100:
                            break
        except (IOError, OSError):
            continue
        if len(results) >= 100:
            break

    if not results:
        return "No matches for pattern '{}' found.".format(pattern)

    result = '\n'.join(results)
    if len(results) >= 100:
        result += "\n... (showing first 100 matches)"
    return result


def tool_run_command(args, project_dir, trust_mode=False):
    """Execute a shell command."""
    command = args.get("command", "")
    if not command.strip():
        return "Error: Empty command."

    if is_dangerous_command(command):
        return "Error: Command blocked — matches a dangerous pattern."

    desc = "Run command: {}".format(command)
    if not confirm_action(desc, trust_mode=trust_mode):
        return "Cancelled: Command was not approved by user."

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT,
            cwd=project_dir,
            encoding='utf-8',
            errors='replace',
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            if output:
                output += "\n--- stderr ---\n"
            output += result.stderr
        if not output:
            output = "(no output)"

        if result.returncode != 0:
            output = "Exit code: {}\n{}".format(result.returncode, output)

        if len(output) > MAX_CMD_OUTPUT:
            output = output[:MAX_CMD_OUTPUT] + "\n... (truncated at {} chars)".format(
                MAX_CMD_OUTPUT)

        return output
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after {} seconds.".format(CMD_TIMEOUT)
    except Exception as e:
        return "Error running command: {}".format(e)


# ── Tool Dispatch ────────────────────────────────────────────────────────────

def execute_tool(name, args, project_dir, trust_mode=False):
    """Execute a tool by name. Returns result string."""
    # Ensure args is a dict (some models send JSON string)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            args = {}

    dispatch = {
        "read_file": lambda: tool_read_file(args, project_dir),
        "write_file": lambda: tool_write_file(args, project_dir, trust_mode),
        "edit_file": lambda: tool_edit_file(args, project_dir, trust_mode),
        "list_files": lambda: tool_list_files(args, project_dir),
        "search_files": lambda: tool_search_files(args, project_dir),
        "run_command": lambda: tool_run_command(args, project_dir, trust_mode),
    }

    handler = dispatch.get(name)
    if handler:
        try:
            return handler()
        except Exception as e:
            return "Error in tool '{}': {}".format(name, e)
    return "Error: Unknown tool '{}'.".format(name)


def summarize_args(name, args):
    """Create a short summary of tool args for display."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return args[:60]

    if name == "read_file":
        s = args.get("path", "?")
        offset = args.get("offset")
        limit = args.get("limit")
        if offset or limit:
            s += " (lines {}-{})".format(
                offset or 1,
                (offset or 1) + (limit or 0) - 1 if limit else "end")
        return s
    elif name in ("write_file", "edit_file"):
        return args.get("path", "?")
    elif name == "list_files":
        return args.get("pattern", "*")
    elif name == "search_files":
        return "/{}/".format(args.get("pattern", "?"))
    elif name == "run_command":
        cmd = args.get("command", "?")
        return cmd[:60] + ("..." if len(cmd) > 60 else "")
    return str(args)[:60]


# ── Ollama Tool Schemas ──────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file. Returns numbered lines. "
                "Always read a file before editing it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the project directory"
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Start line number (1-based). Optional."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of lines to read. Optional."
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write or create a file with the given content. "
                "Creates parent directories if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the project directory"
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete file content to write"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Edit a file by replacing an exact string match. The old_string must "
                "appear exactly once in the file. Include enough surrounding context "
                "to make the match unique."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the project directory"
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact string to find (must be unique in file)"
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The replacement string"
                    }
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files matching a glob pattern. "
                "Use ** for recursive matching (e.g., '**/*.py')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (e.g., '**/*.py', 'src/*.js')"
                    },
                    "path": {
                        "type": "string",
                        "description": "Subdirectory to search in. Optional."
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search file contents using a regex pattern. "
                "Returns matching lines as file:line:content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for"
                    },
                    "path": {
                        "type": "string",
                        "description": "Subdirectory to search in. Optional."
                    },
                    "glob_filter": {
                        "type": "string",
                        "description": "Only search files matching this glob (e.g., '*.py'). Optional."
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command in the project directory. "
                "Use for tests, builds, git, installs, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute"
                    }
                },
                "required": ["command"]
            }
        }
    },
]


# ── System Prompt ────────────────────────────────────────────────────────────

def build_system_prompt(project_dir):
    """Build the system prompt with project context."""
    parts = [
        "You are a coding assistant working in the project directory: {}".format(
            project_dir.replace('\\', '/')),
        "",
    ]

    # Load CLAUDE.md if present
    for candidate in ["CLAUDE.md", ".claude/CLAUDE.md"]:
        path = os.path.join(project_dir, candidate)
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    claude_md = f.read()[:4000]
                parts.append("## Project Instructions (from {})".format(candidate))
                parts.append(claude_md)
                parts.append("")
            except (IOError, OSError):
                pass
            break

    parts.extend([
        "## Tools",
        "You have tools to read, write, edit, list, search files and run commands.",
        "When you call a tool, you will receive the result in the next message.",
        "USE the tool result to complete the user's request — do not ask the user",
        "what to do with it. For example, if asked to 'read X and explain it',",
        "call read_file, then use the returned content to write your explanation.",
        "You may call multiple tools in sequence to accomplish a task.",
        "",
        "## Rules",
        "- Read files before modifying them",
        "- Make minimal, focused changes",
        "- Explain what you're doing before using tools",
        "- Ask the user if requirements are unclear",
        "- Do not modify files outside the project directory",
        "- When editing, include enough context in old_string to ensure a unique match",
        "",
        "Today's date: {}".format(datetime.date.today().isoformat()),
    ])

    return '\n'.join(parts)


# ── Ollama API ───────────────────────────────────────────────────────────────

def ollama_chat(host, port, model, messages, tools=None):
    """Call Ollama /api/chat (non-streaming). Returns assistant message dict."""
    data = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        data["tools"] = tools

    url = "http://{}:{}/api/chat".format(host, port)
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"})

    resp = urllib.request.urlopen(req, timeout=600)
    result = json.loads(resp.read().decode("utf-8"))
    return result.get("message", {})


# ── Local Fallback ───────────────────────────────────────────────────────────

def get_machine_docs_path():
    """Find a directory of per-machine docs (markdown files named after hostnames)
    used to look up a recommended local model for the current machine.

    Configure with the HIVEMIND_MACHINE_DOCS environment variable. Falls back
    to ~/.hivemind/machines/ if the env var is unset. Returns None if no
    directory is found — model selection then uses MODEL_PREFERENCE only.
    """
    env_path = os.environ.get("HIVEMIND_MACHINE_DOCS")
    if env_path and os.path.isdir(env_path):
        return env_path
    default = os.path.expanduser("~/.hivemind/machines")
    if os.path.isdir(default):
        return default
    return None


def get_recommended_model_from_docs():
    """Try to find a recommended model from machine docs."""
    docs_dir = get_machine_docs_path()
    if not docs_dir:
        return None
    hostname = socket.gethostname().lower()
    doc_path = os.path.join(docs_dir, hostname + ".md")
    if not os.path.isfile(doc_path):
        return None
    try:
        with open(doc_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        for line in content.split('\n'):
            if 'recommended' in line.lower() and ':' in line:
                match = re.search(r'(\w[\w.-]+:\w+)', line)
                if match:
                    return match.group(1)
    except (IOError, OSError):
        pass
    return None


def pick_best_local_model(available_models):
    """Pick the best model from locally available models."""
    # Check preference list
    for pref in MODEL_PREFERENCE:
        if pref in available_models:
            return pref
        # Try matching base name with any tag
        base = pref.split(':')[0]
        for m in available_models:
            if m.startswith(base + ':') or m == base:
                return m

    # Fallback: any model with known tool support
    for m in available_models:
        m_base = m.split(':')[0]
        for prefix in TOOL_CAPABLE_PREFIXES:
            if m_base.startswith(prefix):
                return m

    # Last resort: any model
    if available_models:
        return available_models[0]
    return None


def try_start_ollama():
    """Try to start a local Ollama server. Returns True if started."""
    sys.stderr.write("Attempting to start local Ollama...\n")
    try:
        if sys.platform == 'win32':
            candidates = [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
                "ollama",
            ]
            started = False
            for exe in candidates:
                try:
                    subprocess.Popen(
                        [exe, "serve"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=(subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NO_WINDOW),
                    )
                    started = True
                    break
                except (FileNotFoundError, OSError):
                    continue
            if not started:
                sys.stderr.write("Could not find Ollama executable.\n")
                return False
        else:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        # Poll for readiness
        for _ in range(15):
            time.sleep(1)
            models = check_machine("127.0.0.1", 11434, timeout=2)
            if models is not None:
                sys.stderr.write("Local Ollama started successfully.\n")
                return True

        sys.stderr.write("Ollama started but not responding after 15s.\n")
        return False
    except Exception as e:
        sys.stderr.write("Failed to start Ollama: {}\n".format(e))
        return False


def find_target_with_fallback(route_name, config, model_override=None):
    """Find target with local fallback. Returns (machine_name, host, port, model)."""
    # If model override, try to find it on the fleet
    if model_override:
        mname, host, port, model = find_model_anywhere(model_override, config)
        if host:
            return mname, host, port, model
    else:
        # Try fleet routing
        mname, host, port, model = find_target(route_name, config)
        if host:
            return mname, host, port, model

    # Fleet unavailable — try localhost
    sys.stderr.write("No fleet machines reachable. Trying local Ollama...\n")
    models = check_machine("127.0.0.1", 11434, timeout=3)

    if models is None:
        if try_start_ollama():
            models = check_machine("127.0.0.1", 11434, timeout=3)

    if models is None:
        return None, None, None, None

    # If specific model requested, check locally
    if model_override:
        if model_override in models:
            return "localhost", "127.0.0.1", 11434, model_override
        # Try base name match
        base = model_override.split(':')[0]
        for m in models:
            if m.startswith(base + ':') or m == base:
                return "localhost", "127.0.0.1", 11434, m

    # Pick best available local model
    rec = get_recommended_model_from_docs()
    if rec and rec in models:
        model = rec
    else:
        model = pick_best_local_model(models)

    if model:
        sys.stderr.write("Using local model: {}\n".format(model))
        return "localhost", "127.0.0.1", 11434, model

    sys.stderr.write("No suitable models found locally.\n")
    return None, None, None, None


# ── Session Management ───────────────────────────────────────────────────────

def session_dir(project_dir):
    """Get the .hivemind directory for a project."""
    return os.path.join(project_dir, ".hivemind")


def save_session(project_dir, session_data):
    """Save session to .hivemind/session.json."""
    sdir = session_dir(project_dir)
    os.makedirs(sdir, exist_ok=True)
    session_data["updated"] = datetime.datetime.now().isoformat()
    path = os.path.join(sdir, "session.json")
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(session_data, f, indent=2, ensure_ascii=False)
    except (IOError, OSError) as e:
        sys.stderr.write("Warning: Could not save session: {}\n".format(e))


def load_session(project_dir):
    """Load session from .hivemind/session.json. Returns dict or None."""
    path = os.path.join(session_dir(project_dir), "session.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Check if < 24h old
        updated = data.get("updated", data.get("created", ""))
        if updated:
            try:
                ts = datetime.datetime.fromisoformat(updated)
                age = datetime.datetime.now() - ts
                if age.total_seconds() > SESSION_MAX_AGE:
                    return None
            except (ValueError, TypeError):
                pass
        return data
    except (IOError, OSError, json.JSONDecodeError):
        return None


def archive_session(project_dir):
    """Archive current session to a timestamped file."""
    src = os.path.join(session_dir(project_dir), "session.json")
    if not os.path.isfile(src):
        return
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(session_dir(project_dir), "session_{}.json".format(ts))
    try:
        os.rename(src, dst)
    except (IOError, OSError):
        pass


def new_session(project_dir, route, model, machine):
    """Create a new session data dict."""
    return {
        "created": datetime.datetime.now().isoformat(),
        "updated": datetime.datetime.now().isoformat(),
        "route": route,
        "model": model,
        "machine": machine,
        "cwd": project_dir,
        "messages": [],
        "files_modified": [],
    }


def format_age(iso_timestamp):
    """Format an ISO timestamp as a human-readable age string."""
    try:
        ts = datetime.datetime.fromisoformat(iso_timestamp)
        age = datetime.datetime.now() - ts
        hours = int(age.total_seconds() / 3600)
        mins = int((age.total_seconds() % 3600) / 60)
        if hours > 0:
            return "{}h {}m ago".format(hours, mins)
        return "{}m ago".format(mins)
    except (ValueError, TypeError):
        return "unknown"


# ── Agentic Loop ─────────────────────────────────────────────────────────────

def agent_loop(prompt, session, project_dir, host, port, model, tools,
               trust_mode=False):
    """Run one agentic cycle: user prompt -> tool calls -> final response."""
    messages = session["messages"]
    messages.append({"role": "user", "content": prompt})
    depth = 0

    while depth < MAX_DEPTH:
        depth += 1
        sys.stderr.write("  (thinking...)\n")

        try:
            assistant_msg = ollama_chat(host, port, model, messages, tools)
        except urllib.error.URLError as e:
            sys.stderr.write("Connection error: {}\n".format(e))
            break
        except Exception as e:
            sys.stderr.write("Error: {}\n".format(e))
            break

        messages.append(assistant_msg)

        tool_calls = assistant_msg.get("tool_calls", [])
        if not tool_calls:
            content = assistant_msg.get("content", "")
            if content:
                print("\n" + content)
            break

        # Print any text before tool calls
        pre_text = assistant_msg.get("content", "")
        if pre_text:
            print("\n" + pre_text)

        # Execute each tool call
        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "unknown")
            args = func.get("arguments", {})

            print("  [{}] {}".format(name, summarize_args(name, args)))

            result = execute_tool(name, args, project_dir, trust_mode)

            # Track modified files
            if (name in ("write_file", "edit_file")
                    and not result.startswith("Error")
                    and not result.startswith("Cancelled")):
                fpath = args.get("path", "") if isinstance(args, dict) else ""
                if fpath and fpath not in session.get("files_modified", []):
                    session.setdefault("files_modified", []).append(fpath)

            messages.append({"role": "tool", "name": name, "content": result})

        save_session(project_dir, session)

    if depth >= MAX_DEPTH:
        sys.stderr.write("Reached maximum tool depth ({}). Stopping.\n".format(MAX_DEPTH))

    save_session(project_dir, session)


# ── Interactive Mode ─────────────────────────────────────────────────────────

def show_session_info(session):
    """Display session info."""
    print("Session info:")
    print("  Model:    {} on {}".format(
        session.get("model", "?"), session.get("machine", "?")))
    print("  Route:    {}".format(session.get("route", "?")))
    print("  Messages: {}".format(len(session.get("messages", []))))
    print("  Created:  {}".format(session.get("created", "?")))
    print("  Updated:  {}".format(session.get("updated", "?")))
    files = session.get("files_modified", [])
    if files:
        print("  Modified: {}".format(", ".join(files)))


def interactive_loop(session, project_dir, config, host, port, model, route,
                     trust_mode=False, no_triage=False, escalate=False):
    """Main interactive loop."""
    system_prompt = build_system_prompt(project_dir)

    # Add system prompt if starting fresh
    if not session["messages"]:
        session["messages"].append({"role": "system", "content": system_prompt})

    esc_tag = " [escalate]" if escalate else ""
    print("Hivemind Code v{} — {} on {} ({}:{}){}".format(
        VERSION, model, session.get("machine", "?"), host, port, esc_tag))
    print("Commands: /r <route>  /m <model>  /status  /session  /clear  /save  /quit")
    print("Project: {}".format(project_dir))
    print()

    while True:
        try:
            prompt = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSaving session...")
            save_session(project_dir, session)
            break

        if not prompt:
            continue

        # Handle slash commands
        if prompt.startswith('/'):
            cmd_parts = prompt.split(None, 1)
            cmd = cmd_parts[0].lower()
            cmd_arg = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""

            if cmd in ('/quit', '/exit', '/q'):
                save_session(project_dir, session)
                print("Session saved. Bye!")
                break

            elif cmd == '/status':
                show_status(config)
                continue

            elif cmd == '/session':
                show_session_info(session)
                continue

            elif cmd == '/clear':
                archive_session(project_dir)
                session["messages"] = [
                    {"role": "system", "content": system_prompt}]
                session["files_modified"] = []
                save_session(project_dir, session)
                print("Conversation cleared.")
                continue

            elif cmd == '/save':
                save_session(project_dir, session)
                print("Session saved.")
                continue

            elif cmd == '/r':
                if not cmd_arg:
                    print("Usage: /r <route> — routes: quick, interactive, quality")
                    continue
                new_route = cmd_arg
                mname, h, p, m = find_target_with_fallback(new_route, config)
                if h:
                    host, port, model, route = h, p, m, new_route
                    session["route"] = route
                    session["model"] = model
                    session["machine"] = mname
                    save_session(project_dir, session)
                    print("Switched to {} on {} ({}:{})".format(
                        model, mname, host, port))
                else:
                    print("No machine available for route '{}'.".format(new_route))
                continue

            elif cmd == '/m':
                if not cmd_arg:
                    print("Usage: /m <model>")
                    continue
                new_model = cmd_arg
                mname, h, p, m = find_target_with_fallback(
                    route, config, model_override=new_model)
                if h:
                    host, port, model = h, p, m
                    session["model"] = model
                    session["machine"] = mname
                    save_session(project_dir, session)
                    print("Switched to {} on {} ({}:{})".format(
                        model, mname, host, port))
                else:
                    print("Model '{}' not found on any machine.".format(new_model))
                continue

            elif cmd == '/compact':
                print("(Compact is not yet implemented — coming in v2)")
                continue

            else:
                print("Unknown command: {}. Try /quit /r /m /status /session /clear /save".format(cmd))
                continue

        # Check for escalation before running
        if escalate and not no_triage:
            suggested = classify(prompt, config)
            current_rank = ROUTE_RANK.get(route, 1)
            suggested_rank = ROUTE_RANK.get(suggested, 1)
            if suggested_rank > current_rank:
                mname, h, p, m = find_target_with_fallback(suggested, config)
                if h:
                    host, port, model, route = h, p, m, suggested
                    session["route"] = route
                    session["model"] = model
                    session["machine"] = mname
                    save_session(project_dir, session)
                    print("  ^ Escalating to {} on {} [{}]\n".format(
                        model, mname, suggested))

        # Regular prompt — run agent loop
        print()
        agent_loop(prompt, session, project_dir, host, port, model, TOOLS,
                   trust_mode)
        print()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Hivemind Code — Local AI Coding Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s                                  # interactive mode\n"
            "  %(prog)s \"add error handling to main.py\"  # initial prompt\n"
            "  %(prog)s -r quality \"refactor auth\"       # force quality route\n"
            "  %(prog)s --resume                         # resume last session\n"
            "  %(prog)s --trust \"run the tests\"          # auto-approve\n"
            "  %(prog)s --escalate                       # auto-upgrade on hard prompts\n"
            "  %(prog)s --one-shot \"explain this\"        # respond and exit\n"
        )
    )
    parser.add_argument("prompt", nargs="*", help="Initial prompt (optional)")
    parser.add_argument("-r", "--route",
                        help="Force route: quick, interactive, quality")
    parser.add_argument("-m", "--model",
                        help="Force specific model (skip routing)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume previous session")
    parser.add_argument("--trust", action="store_true",
                        help="Auto-approve file writes and commands")
    parser.add_argument("--one-shot", action="store_true",
                        help="Non-interactive mode (respond and exit)")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="Path to hivemind-config.json")
    parser.add_argument("--no-triage", action="store_true",
                        help="Skip triage, use interactive route by default")
    parser.add_argument("--escalate", action="store_true",
                        help="Auto-escalate to stronger models when task "
                             "complexity increases (never auto-downgrades)")
    parser.add_argument("--version", action="version",
                        version="hivemind-code " + VERSION)

    args = parser.parse_args()

    # Load fleet config
    try:
        config = load_config(args.config)
    except (IOError, OSError, json.JSONDecodeError) as e:
        sys.stderr.write("Warning: Could not load config: {}\n".format(e))
        sys.stderr.write("Continuing with local-only mode.\n")
        config = {"machines": {}, "routes": {},
                  "triage": {"default": "interactive"}}

    project_dir = os.getcwd()

    # Determine route
    initial_prompt = " ".join(args.prompt) if args.prompt else ""

    if args.model:
        route = "direct"
    elif args.route:
        route = args.route
    elif args.no_triage or not initial_prompt:
        route = "interactive"
    else:
        sys.stderr.write("Classifying... ")
        route = classify(initial_prompt, config)
        sys.stderr.write("[{}]\n".format(route))

    # Find target machine/model
    mname, host, port, model = find_target_with_fallback(
        route, config, model_override=args.model)

    if not host:
        sys.stderr.write(
            "Error: No Ollama instance available "
            "(fleet offline, no local server).\n"
            "Install Ollama (https://ollama.ai) or check fleet config.\n")
        sys.exit(1)

    sys.stderr.write("{} on {} ({}:{})\n".format(model, mname, host, port))

    # Session handling
    session = None

    if args.resume:
        session = load_session(project_dir)
        if session:
            msg_count = len(session.get("messages", []))
            files = session.get("files_modified", [])
            updated = session.get("updated", "")
            age_str = format_age(updated) if updated else "unknown"
            print("Resuming: {} messages, last active {}".format(
                msg_count, age_str))
            if files:
                print("Modified: {}".format(", ".join(files)))
            session["model"] = model
            session["machine"] = mname
            session["route"] = route
        else:
            sys.stderr.write("No resumable session found. Starting fresh.\n")

    if not session:
        # Check for existing session to offer resume
        existing = load_session(project_dir)
        if existing:
            msg_count = len(existing.get("messages", []))
            if msg_count > 1:  # More than just system prompt
                try:
                    choice = input(
                        "Continue previous session ({} messages)? [y/N]: "
                        .format(msg_count)).strip().lower()
                    if choice in ('y', 'yes'):
                        session = existing
                        session["model"] = model
                        session["machine"] = mname
                        session["route"] = route
                    else:
                        archive_session(project_dir)
                except (EOFError, KeyboardInterrupt):
                    print()
                    archive_session(project_dir)

    if not session:
        session = new_session(project_dir, route, model, mname)

    # One-shot mode
    if args.one_shot and initial_prompt:
        system_prompt = build_system_prompt(project_dir)
        if not session["messages"]:
            session["messages"].append(
                {"role": "system", "content": system_prompt})
        agent_loop(initial_prompt, session, project_dir, host, port, model,
                   TOOLS, args.trust)
        return

    # Interactive mode with initial prompt
    if initial_prompt:
        system_prompt = build_system_prompt(project_dir)
        if not session["messages"]:
            session["messages"].append(
                {"role": "system", "content": system_prompt})

        esc_tag = " [escalate]" if args.escalate else ""
        print("Hivemind Code v{} — {} on {} ({}:{}){}".format(
            VERSION, model, mname, host, port, esc_tag))
        print("Commands: /r <route>  /m <model>  /status  /session  "
              "/clear  /save  /quit")
        print("Project: {}".format(project_dir))
        print()
        print("You: {}".format(initial_prompt))
        print()
        agent_loop(initial_prompt, session, project_dir, host, port, model,
                   TOOLS, args.trust)
        print()

        # Continue interactively
        interactive_loop(session, project_dir, config, host, port, model,
                         route, args.trust, args.no_triage, args.escalate)
    else:
        interactive_loop(session, project_dir, config, host, port, model,
                         route, args.trust, args.no_triage, args.escalate)


if __name__ == "__main__":
    main()

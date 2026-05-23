# Hivemind

Local AI model router and agentic coding assistant for a personal Ollama fleet. Stdlib-only Python, no pip dependencies.

```bash
hm  "explain python decorators"            # router: classifies → picks the right model → streams reply
hmc "refactor the auth module"             # coding agent: reads/writes files, runs commands, persists sessions
```

## Why this exists

I have a couple of machines with discrete GPUs sitting idle most of the day. I wanted Claude-Code-style agentic capabilities entirely on my own hardware — partly for cost, partly because some work shouldn't leave the LAN. Hivemind is the result. It does three things that turned out to matter:

1. **Triages every prompt to the right model.** A tiny classifier (`granite3-moe:1b`, ~5s) labels each prompt `quick` / `interactive` / `quality`, and the router picks the best available model on the LAN for that tier. You don't pay 70s of `gpt-oss:20b` for a one-liner.
2. **Falls back gracefully when machines are off.** Fleet unreachable → try localhost → no local Ollama → start one. The assistant works on any machine with Ollama installed; the fleet is an accelerator, not a dependency.
3. **Treats coding work as agentic, not chat.** `hmc` exposes six tools (read/write/edit/list/search files, run commands) with diff-preview confirmation, a 24-hour resumable session, and an `--escalate` mode that upgrades models when triage decides the task got harder.

It's a personal tool. Use it, fork it, ignore it. License is MIT.

## Install

```bash
python install.py          # adds this folder to PATH
python install.py --check  # verify installation
python install.py --remove # uninstall
```

Open a new terminal after installing. The commands `hm` and `hmc` become available everywhere.

Copy `hivemind-config.example.json` to `hivemind-config.json` and edit it for your own fleet. The real config is gitignored.

## hm — Router

Routes prompts through a triage classifier to the best available model.

```bash
hm "explain python decorators"          # auto-classify and route
hm -r quality "design a REST API"       # force quality route
hm -m gpt-oss:20b "hard question"       # force specific model
hm -i                                   # interactive chat
hm --status                             # check fleet status
echo "prompt" | hm                      # pipe input
```

### Routes

| Route | Model | Speed | Use case |
|-------|-------|-------|----------|
| quick | qwen2.5-coder:3b | ~4s | Syntax, one-liners, lookups |
| interactive | phi4:14b | ~18s | Explanations, moderate code, debugging |
| quality | gpt-oss:20b | ~70s | Architecture, complex debugging, large code |

Triage uses `granite3-moe:1b` (~5s) to classify prompt complexity. Each route has a fallback chain — if the primary machine is offline, it tries the next.

### Flags

| Flag | Description |
|------|-------------|
| `-r <route>` | Force route: `quick`, `interactive`, `quality` |
| `-m <model>` | Force specific model on any reachable machine |
| `-s <text>` | Custom system prompt |
| `-i` | Interactive mode |
| `--status` | Show fleet machine status |
| `--config <path>` | Custom config file path |

## hmc — Coding Assistant

Agentic tool loop: reads files, writes code, runs commands, maintains conversation context.

```bash
hmc                                    # interactive coding session
hmc "add error handling to main.py"    # start with a prompt
hmc -r quality "refactor the auth"     # force quality route
hmc --escalate                         # auto-upgrade on hard tasks
hmc --trust "run the tests"            # auto-approve file writes/commands
hmc --resume                           # resume previous session
hmc --one-shot "explain this codebase" # respond and exit
```

### Tools (6)

| Tool | Description | Confirmation |
|------|-------------|--------------|
| `read_file` | Read file with line numbers | No |
| `write_file` | Create or overwrite a file | Yes (diff view) |
| `edit_file` | Find-and-replace edit | Yes (diff view) |
| `list_files` | Glob pattern file search | No |
| `search_files` | Regex content search | No |
| `run_command` | Shell command execution | Yes |

Write/edit/command confirmations show what will happen and prompt `[y]es / [n]o / [v]iew diff`. Use `--trust` to auto-approve.

### Interactive Commands

| Command | Description |
|---------|-------------|
| `/r <route>` | Switch route (keeps conversation context) |
| `/m <model>` | Switch to specific model (keeps context) |
| `/status` | Show fleet machine status |
| `/session` | Show session info (messages, model, files) |
| `/clear` | Clear conversation, start fresh |
| `/save` | Force save session |
| `/quit` | Save and exit |

### Escalation Mode

With `--escalate`, the assistant starts on a fast model and automatically upgrades when tasks get harder:

```
$ hmc --escalate
Hivemind Code v0.1.0 — phi4:14b on laptop [escalate]

You: what files are here?                   # stays on phi4 (fast)
You: explain the auth module                # stays on phi4
You: redesign auth to use JWT with refresh  # triage detects complexity
  ^ Escalating to gpt-oss:20b on laptop [quality]
You: now add tests for it                   # stays on gpt-oss (never downgrades)
```

The triage call adds ~5s overhead per prompt. Use `/r interactive` to manually step back down.

### Session Persistence

Sessions are saved to `.hivemind/session.json` in the project directory:

- Auto-saved after each tool loop iteration (crash-safe)
- On startup, offers to resume if a session exists and is < 24h old
- `--resume` flag skips the prompt and resumes directly
- Model switching preserves full conversation history
- Archived sessions kept as `.hivemind/session_{timestamp}.json`

### Project Awareness

If a `CLAUDE.md` or `.claude/CLAUDE.md` file exists in the project directory, its contents are loaded into the system prompt (up to 4000 chars). This lets you set project-specific instructions that the model follows.

### Recommended-model docs (optional)

If `HIVEMIND_MACHINE_DOCS` points to a directory containing one markdown file per machine (named `<hostname>.md`), `hmc` will look for a line like `Recommended: phi4:14b` in that machine's file and prefer it during local fallback. Falls back to `~/.hivemind/machines/` if the env var is unset. If neither exists, model selection uses the built-in preference list only.

### Flags

| Flag | Description |
|------|-------------|
| `-r <route>` | Force route: `quick`, `interactive`, `quality` |
| `-m <model>` | Force specific model |
| `--escalate` | Auto-escalate to stronger models on hard prompts |
| `--trust` | Auto-approve all file writes and commands |
| `--resume` | Resume previous session |
| `--one-shot` | Non-interactive mode (respond then exit) |
| `--no-triage` | Skip triage, default to interactive |
| `--config <path>` | Custom config file path |

### Safety

- **Path containment:** Cannot read/write files outside the project directory
- **Write confirmation:** All writes show content/diff before applying
- **Command blocklist:** Rejects `rm -rf /`, `format C:`, and other destructive patterns
- **Binary detection:** Refuses to read binary files
- **Output truncation:** File reads capped at 50K chars, command output at 10K chars

## Fleet Configuration

Edit `hivemind-config.json` to define your machines and routes (see `hivemind-config.example.json` for a template):

```json
{
  "machines": {
    "laptop": {
      "host": "192.168.1.11",
      "port": 11434,
      "label": "Laptop with discrete GPU"
    },
    "desktop": {
      "host": "192.168.1.10",
      "port": 11434,
      "label": "Desktop with discrete GPU"
    }
  },
  "triage": {
    "machine": "desktop",
    "model": "granite3-moe:1b",
    "default": "interactive"
  },
  "routes": {
    "quick": [
      {"machine": "laptop", "model": "qwen2.5-coder:3b"},
      {"machine": "desktop", "model": "qwen2.5-coder:3b"}
    ],
    "interactive": [
      {"machine": "laptop", "model": "phi4:14b"},
      {"machine": "desktop", "model": "qwen2.5-coder:7b"}
    ],
    "quality": [
      {"machine": "laptop", "model": "gpt-oss:20b"},
      {"machine": "desktop", "model": "gpt-oss:20b"}
    ]
  }
}
```

Each route lists machines in priority order. If the first is offline, it tries the next.

## Local Fallback

When no fleet machines are reachable, `hmc` falls back to localhost:

1. Check `localhost:11434` for a running Ollama
2. If not running, attempt to start it (`ollama serve` in background)
3. Wait up to 15s for it to come online
4. Pick the best available model from a preference list:
   `gpt-oss:20b > phi4:14b > qwen2.5-coder:7b > qwen2.5-coder:3b > any tool-capable model`

This means `hmc` works on any machine with Ollama installed — it just uses the fleet when available for better speed and model selection.

## Adding a New Machine

1. Install Ollama on the machine
2. Set environment variable: `OLLAMA_HOST=0.0.0.0:11434` (machine-level)
3. Add firewall rule: TCP port 11434 inbound
4. Restart Ollama
5. Pull models: `ollama pull phi4:14b` (or whichever you want)
6. Add the machine to `hivemind-config.json`
7. Verify: `hm --status`

## Tool-Capable Models

The coding assistant requires models that support Ollama's tool calling API. Known to work:

- `qwen2.5-coder` (3b, 7b, 14b, 32b)
- `gpt-oss:20b`
- `qwen3-coder:30b`
- `devstral-small-2:24b`
- `phi4:14b`

## MCP server (optional)

`hivemind_mcp.py` exposes the same six tools (`read_file`, `write_file`, `edit_file`, `list_files`, `search_files`, `run_command`) as a Model Context Protocol server over stdio. Register with Claude Desktop or any MCP-aware client.

```bash
pip install mcp
```

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hivemind": {
      "command": "python",
      "args": ["C:/absolute/path/to/hivemind_mcp.py"]
    }
  }
}
```

Trust mode is forced on for MCP — confirmation prompts can't run over stdio. The other safety rails (path containment, dangerous-command blocklist) still apply.

See [`docs/MCP_WRAPPER_SPEC.md`](docs/MCP_WRAPPER_SPEC.md) for the implementation spec and [`docs/MCP_EXPERIMENT.md`](docs/MCP_EXPERIMENT.md) for the (mostly failed) experiment in having `hmc` write the wrapper from its own spec.

## Files

| File | Description |
|------|-------------|
| `hivemind.py` | Router — triage, routing, streaming |
| `hivemind-code.py` | Coding assistant — tools, agent loop, sessions |
| `hivemind_mcp.py` | MCP server exposing the six tools over stdio |
| `hivemind-config.example.json` | Fleet config template — copy to `hivemind-config.json` |
| `install.py` | Cross-platform PATH installer |
| `hm` / `hm.bat` | Router command wrapper (Unix / Windows) |
| `hmc` / `hmc.bat` | Coding assistant command wrapper (Unix / Windows) |

## Requirements

- Python 3.8+
- Ollama (on at least one machine)
- No pip dependencies — stdlib only

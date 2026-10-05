# Hivemind

Local AI model router and agentic coding assistant for a personal Ollama fleet. Stdlib-only Python, no pip dependencies.

```bash
hm  "explain python decorators"            # router: classifies → picks the right model → streams reply
hmc "refactor the auth module"             # coding agent: reads/writes files, runs commands, persists sessions
```

`hivemind_mcp.py` gives the same models to Claude Code (or any MCP client) as tools — see [Use the hivemind from Claude Code (MCP)](#use-the-hivemind-from-claude-code-mcp).

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

The choice of model per route is informed by [`ollama-benchmark`](https://github.com/jblenman/ollama-benchmark) — see its [FINDINGS.md](https://github.com/jblenman/ollama-benchmark/blob/main/FINDINGS.md) for the data behind these picks (33 prompts × 9 categories × 24 models, CPU and GPU).

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
      "host": "10.0.0.11",
      "port": 11434,
      "label": "Laptop with discrete GPU"
    },
    "desktop": {
      "host": "10.0.0.10",
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

## Use the hivemind from Claude Code (MCP)

`hivemind_mcp.py` is an MCP server — stdio, standard library only, nothing to `pip install` — that hands your Ollama hosts to Claude Code as tools. Claude Desktop and other MCP clients can use it too. It reads the same `hivemind-config.json` as `hm` and `hmc`; with no config at all it uses the Ollama on this machine (`localhost:11434`).

| Tool | What it does |
|------|--------------|
| `health` | Which configured hosts answer: latency, Ollama version, model count, models loaded in memory. Also the routing tiers and the config file the server read. Call it first when another tool fails. |
| `list_models(host?)` | Models on one host: size (GB), parameters, quantization, family. |
| `ask_local(prompt, model?, host?, system?, max_tokens?, temperature?, timeout_s?)` | One non-streaming completion. Default model: the host's `quick` model from the config. Returns the text plus model, host, token counts, durations and tokens/s. |
| `route(prompt, tier?, system?, max_tokens?, temperature?, timeout_s?)` | `hm`'s routing: tier `quick`, `interactive`, `quality`, or `auto` (the triage model picks, as in `hm`). Walks the tier's chain, skipping hosts that don't answer or don't have the model, and reports who answered and the `fallback_path`. |
| `embed(text, model?, host?, round?, include_vector?)` | An embedding vector, default model `nomic-embed-text` (768 dimensions). |

All five are read-only. `host` accepts a machine name from your config, `localhost`, or a private-network address (an RFC 1918 or link-local IP, a `.local`, `.lan` or `.home.arpa` name, or a bare host name); anything else is refused, so a tool call can't send your prompt to an arbitrary internet host. A machine you list in the config is always allowed, whatever its address.

**Local models are advisory.** The server's MCP instructions tell Claude to use local output only where a mistake is caught or harmless — drafts, summaries it will check, classification suggestions, embeddings, code it will run tests against — and never as a gate or as a judge of its own work: a small model can report success on work it never did ([docs/MCP_EXPERIMENT.md](docs/MCP_EXPERIMENT.md) has a worked example). Every completion carries `"advisory": "local-model output: verify before acting on it"`, and an empty answer comes back with a `warning` — treat it as a failure, not an answer.

### Install

Choose **one** of A or B (both at once gives Claude two copies of every tool).

**A. Register the server** — works with any Claude Code setup. Use the full path to your clone:

```bash
claude mcp add --scope user hivemind -- python3 /path/to/hivemind/hivemind_mcp.py
```

```powershell
# Windows (PowerShell) — see "Windows interpreter" below if py is a Store/PyManager alias
claude mcp add --scope user hivemind -- py "C:\path\to\hivemind\hivemind_mcp.py"
```

The tools appear as `mcp__hivemind__<tool>`.

**B. Install it as a plugin.** The repository is a Claude Code plugin (`.claude-plugin/plugin.json` plus `.mcp.json`).

- One session, nothing installed: `claude --plugin-dir /path/to/hivemind`
- Permanently, from a plugin marketplace that lists this repository: `claude plugin install hivemind@<marketplace>`. To list it in a marketplace you maintain, add this entry to its `marketplace.json`:

  ```json
  { "name": "hivemind", "source": { "source": "github", "repo": "jblenman/hivemind" },
    "description": "Your Ollama models as Claude Code tools: health, list_models, ask_local, route, embed." }
  ```

The tools appear as `mcp__plugin_hivemind_hivemind__<tool>`. A plugin installed from GitHub is copied into Claude Code's plugin cache, and your `hivemind-config.json` (not in git) does not travel with it — put the config in `~/.hivemind/hivemind-config.json`. Installs from a marketplace update when the `version` in `.claude-plugin/plugin.json` changes.

**Claude Desktop and other clients** — add to the client's MCP config (for Claude Desktop, `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "hivemind": {
      "command": "python3",
      "args": ["/path/to/hivemind/hivemind_mcp.py"]
    }
  }
}
```

**Working inside a clone of this repository?** Claude Code also reads the repo's `.mcp.json` as a project server and offers to approve it (`claude mcp list` warns `Missing environment variables: CLAUDE_PLUGIN_ROOT`). That file is the plugin's configuration and only works when loaded as a plugin, so decline it and use `claude --plugin-dir .` or option A.

### Configuration

The server uses the first config file it finds:

1. `--config PATH` (an argument after the script path)
2. `$HIVEMIND_CONFIG`
3. `hivemind-config.json` next to `hivemind_mcp.py` — the file `hm` and `hmc` use
4. `~/.hivemind/hivemind-config.json` — the place for plugin installs
5. none found: one machine, `localhost` (127.0.0.1:11434), with the models of `hivemind-config.example.json`

The format is the one in [Fleet Configuration](#fleet-configuration). One optional key is read only by the MCP server: `"default_machine": "<name>"`, the machine used when a tool gets no `host` (default: the first machine listed). `health` reports which file was used.

| Environment variable | Effect |
|----------------------|--------|
| `HIVEMIND_CONFIG` | Path of the config file (checked after `--config`). |
| `HIVEMIND_MCP_LOG` | Log file path, or `off`. Default `~/.hivemind/mcp_server.log`, rotated at 2 MB. |
| `HIVEMIND_MCP_DEBUG` | `1` adds debug detail to the log and to stderr. |
| `KIT_PYTHON` | Interpreter the plugin's `.mcp.json` runs; default `python3`. A shared name, so one setting can serve several plugins. |

For option A, pass variables with `claude mcp add --env NAME=value ...`; Claude Code also passes its own environment to the servers it starts.

**Windows interpreter.** `python3` on Windows is usually missing or a Microsoft Store / PyManager alias stub, which can fail to start from elevated shells. For the plugin, set the interpreter in `~/.claude/settings.json` (Claude Code's `env` setting reaches the plugin's `.mcp.json`):

```json
{ "env": { "KIT_PYTHON": "py" } }
```

Use `py` where it is the classic launcher (`C:\Windows\py.exe`, a regular file). Where `py` is itself an app-execution alias (`(Get-Item (where.exe py)).Attributes` shows `ReparsePoint`), use the PyManager launcher instead, with forward slashes: `"C:/Users/<you>/AppData/Local/Python/bin/python.exe"`. Such aliases can be refused across logon sessions — seen on Windows 11 with a PyManager Python: over SSH, `py` answered "Access is denied" once a desktop session had started MCP servers through it. Don't use a versioned interpreter path; it breaks on the next Python upgrade.

### Verify it works

```bash
python3 hivemind_mcp.py --self-test   # offline; stderr shows "tools: health, list_models, ask_local, route, embed" and the config used
python3 hivemind_mcp.py --health      # which hosts answer, as JSON on stderr
claude mcp get hivemind               # option A: "Status: ✔ Connected"
claude -p "Call the hivemind health tool once and list the hosts that answer." --allowedTools mcp__hivemind
python3 tests/test_mcp_server.py      # 47 tests; the live ones skip when no Ollama answers
```

For option B, run the `claude -p` line with `--plugin-dir /path/to/hivemind` and `--allowedTools mcp__plugin_hivemind_hivemind`. Put the prompt before `--allowedTools`: that option takes several values and would swallow a prompt placed after it. The live tests use `localhost:11434` (or your config's default machine); point them elsewhere with `HIVEMIND_TEST_OLLAMA=host:port`, and set `HIVEMIND_TEST_MODEL` if `granite3-moe:1b` isn't installed.

### Permissions

- **Interactive sessions** follow your permission mode: Manual mode asks before a tool runs, auto mode leaves it to its classifier. To allow the server's tools outright, add the server to `permissions.allow` in your settings: `"mcp__hivemind"` (option A) or `"mcp__plugin_hivemind_hivemind"` (option B).
- **Headless runs** (`claude -p`) can't ask, so pass the same rule: `--allowedTools mcp__hivemind` or `--allowedTools mcp__plugin_hivemind_hivemind`. Without it the call is refused ("Claude requested permissions to use mcp__plugin_hivemind_hivemind__health, but you haven't granted it yet").
- `--strict-mcp-config` leaves out plugin servers — only the servers in your `--mcp-config` file load, even with `--plugin-dir` — so in a strict run put the server in that file, with the same `mcpServers` entry as for Claude Desktop above.

### What the server does not do

- **It does not start Ollama.** Unlike `hmc`, which runs `ollama serve` when nothing answers, the server only reports: `health` shows which hosts are down. Start Ollama yourself.
- It does not pull, delete or modify models, and does not stream: each call returns when the answer is complete.
- It handles one request at a time; a long generation (up to `timeout_s`, default 120 s, maximum 900 s) holds up the next call.
- It contacts only configured machines, `localhost` and private-network addresses.
- It adds no authentication or encryption: it speaks plain HTTP to Ollama, which has neither. Anyone who can reach a host's port 11434 can use its models — that exposure comes from how you run Ollama (see [Adding a New Machine](#adding-a-new-machine)), not from the server.
- It does not check what the models say.

### Code tools (optional, off by default)

`--code-tools` adds `hmc`'s six tools — `read_file`, `write_file`, `edit_file`, `list_files`, `search_files`, `run_command` — for MCP clients that have no file tools of their own, such as Claude Desktop. Claude Code has its own file and shell tools; you don't need these there.

```json
"args": ["/path/to/hivemind/hivemind_mcp.py", "--code-tools", "--project-dir", "/path/to/your/project"]
```

- They act inside one project directory: `--project-dir`, else `$CLAUDE_PROJECT_DIR` (Claude Code sets it for the servers it starts), else the server's working directory. A filesystem root or your home folder is refused — the server then starts without the code tools and `health` says why.
- `hmc`'s rails apply: paths are contained to the project directory, binary files are refused, output is truncated, and a short blocklist rejects commands such as `rm -rf /`. Writes and commands run without `hmc`'s confirmation prompt, which can't work over stdio.
- `write_file`, `edit_file` and `run_command` are flagged `_meta["anthropic/requiresUserInteraction"]`, so Claude Code asks a person on **every** call — according to its documentation even in auto mode and when an allow rule matches — and refuses them in headless runs. Checked on 2.1.289: with `--allowedTools mcp__hivemind`, `read_file` ran and `write_file` was refused ("MCPTool requires permission"); the same run without the flag wrote the file. The three read tools follow your normal rules.
- **Upgrading from the earlier `hivemind_mcp.py`**, which served only these six tools through the `mcp` package: add `--code-tools --project-dir <dir>` to the server's arguments. `pip install mcp` is no longer needed. Two defects of that version are fixed: in trust mode `hmc` prints `[auto-approved] …` lines, which landed on the protocol stream, and `run_command` let the command inherit the server's stdin — the protocol stream — so a command that read input could swallow the client's next messages.

### Protocol and operations

Behaviour noted here was checked with Claude Code 2.1.289 on macOS.

- Transport: MCP stdio, newline-delimited JSON-RPC 2.0 (`Content-Length` framing is tolerated too). Nothing but JSON-RPC goes to stdout.
- Protocol versions: echoes `2025-11-25` (what Claude Code 2.1.289 asks for), `2025-06-18`, `2025-03-26` and `2024-11-05`; any other request gets `2025-03-26`.
- Methods: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`; anything else gets "method not found" (-32601). A Claude Code 2.1.289 session sends `server/discover` before `initialize`; that answer is expected, and the session then connects normally.
- Claude Code records everything a server writes to stderr as an error, so the server writes only warnings and errors there. The full record is the log file (`HIVEMIND_MCP_LOG`).
- Claude Code stops stdio servers with SIGINT (then SIGTERM and SIGKILL); the server exits cleanly on the first.
- Timeouts: 5 s to connect to a host; generation 120 s by default (`timeout_s`, up to 900 s — a large model only partly offloaded to the GPU can need 300 s or more); triage 30 s.
- Empty answers: on an 8 GB AMD GPU running a community ROCm build of Ollama, MoE models larger than about 6 GB returned an empty response in under two seconds instead of an error. The tools flag every empty response.

### Removing it

- Option A: `claude mcp remove hivemind --scope user`
- Option B: `claude plugin uninstall hivemind@<marketplace>` (or stop passing `--plugin-dir`)
- Other clients: delete the `hivemind` entry from their MCP config.
- Apart from what the code tools write in their project directory, the server writes only its log, `~/.hivemind/mcp_server.log` (and `.log.1`). `~/.hivemind/` may also hold your config and `hmc`'s machine docs.

The first MCP version of this file, a wrapper around the six coding tools, was the subject of an experiment in having `hmc` write it from a spec: [`docs/MCP_WRAPPER_SPEC.md`](docs/MCP_WRAPPER_SPEC.md) and [`docs/MCP_EXPERIMENT.md`](docs/MCP_EXPERIMENT.md).

## Files

| File | Description |
|------|-------------|
| `hivemind.py` | Router — triage, routing, streaming |
| `hivemind-code.py` | Coding assistant — tools, agent loop, sessions |
| `hivemind_mcp.py` | MCP server: the models as tools (`health`, `list_models`, `ask_local`, `route`, `embed`); `hmc`'s six tools with `--code-tools` |
| `.claude-plugin/plugin.json`, `.mcp.json` | Claude Code plugin manifest and its MCP server entry |
| `tests/test_mcp_server.py` | Tests for the MCP server (stdlib `unittest`) |
| `hivemind-config.example.json` | Fleet config template — copy to `hivemind-config.json` |
| `install.py` | Cross-platform PATH installer |
| `hm` / `hm.bat` | Router command wrapper (Unix / Windows) |
| `hmc` / `hmc.bat` | Coding assistant command wrapper (Unix / Windows) |
| `docs/` | Notes on the first MCP wrapper and the experiment around it |

## Requirements

- Python 3.8+
- Ollama (on at least one machine)
- No pip dependencies — stdlib only, the MCP server included

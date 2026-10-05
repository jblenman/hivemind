#!/usr/bin/env python3
"""
hivemind_mcp.py - the hivemind as an MCP server (stdio, stdlib only).

Gives Claude Code, Claude Desktop or any other MCP client the Ollama hosts
listed in hivemind-config.json, the same file `hm` and `hmc` read:

    health       which hosts answer: latency, Ollama version, models loaded
    list_models  the models installed on one host
    ask_local    one completion from a named model on a named host
    route        hm's routing: tier quick | interactive | quality, or auto
                 (the triage model picks), falling down the tier's chain
    embed        an embedding vector (default model nomic-embed-text)

All five are read-only. Small local models are advisory: their output is a
draft to check, never a gate or a judge of its own work (docs/MCP_EXPERIMENT.md
shows a local model reporting success on work it never did). The server tells
its client so in the MCP `instructions`.

--code-tools adds hmc's six tools (read_file, write_file, edit_file,
list_files, search_files, run_command) for clients that have no file tools of
their own, such as Claude Desktop. They act inside one project directory with
hmc's safety rails, and write_file, edit_file and run_command ask for approval
on every call in Claude Code. Off by default.

Transport: MCP stdio - newline-delimited JSON-RPC 2.0 on stdin/stdout.
Nothing but JSON-RPC is written to stdout. Warnings and errors go to stderr;
the full log goes to ~/.hivemind/mcp_server.log.

Config (the first file found wins): --config PATH, $HIVEMIND_CONFIG,
hivemind-config.json next to this file, ~/.hivemind/hivemind-config.json.
With none of them the server uses one host: localhost (127.0.0.1:11434).

    python3 hivemind_mcp.py                   serve on stdio (what MCP clients run)
    python3 hivemind_mcp.py --self-test       offline check: handshake + tool list
    python3 hivemind_mcp.py --health          which configured hosts answer
    python3 hivemind_mcp.py --code-tools [--project-dir DIR]

Importable: handle(message) -> response drives the server without a
subprocess (the tests use it). Python 3.8+, no dependencies.
"""

import argparse
import contextlib
import http.client
import importlib.util
import io
import ipaddress
import json
import os
import socket
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

SERVER_NAME = "hivemind"
SERVER_VERSION = "0.2.0"

# Protocol versions we echo back; anything else is answered with the fallback.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
FALLBACK_PROTOCOL_VERSION = "2025-03-26"

CONNECT_TIMEOUT_S = 5.0          # per host, every request
TAGS_TIMEOUT_S = 5.0             # /api/tags, /api/ps, /api/version reads
DEFAULT_GENERATE_TIMEOUT_S = 120.0
MAX_GENERATE_TIMEOUT_S = 900.0
TRIAGE_TIMEOUT_S = 30.0
DEFAULT_MAX_TOKENS = 1024
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_PORT = 11434
THINKING_PREVIEW_CHARS = 2000
ADVISORY = "local-model output: verify before acting on it"

INSTRUCTIONS = (
    "hivemind gives you small open models served by Ollama on the hosts in hivemind-config.json "
    "(default: localhost:11434). CAUTION: small local models are for ADVISORY and VERIFIABLE work "
    "only. Their self-reports are not trustworthy - a local model can confidently report success on "
    "work it never did - so never use a local model as a gate, as a judge of its own output, or as "
    "a decision-maker whose wrong answer would go unnoticed. Use their output where an error is "
    "caught or harmless: drafts, classification suggestions, summaries you will check, embeddings, "
    "code you will run tests against. Tools: health (which hosts answer; call it first when another "
    "tool fails), list_models, ask_local (one completion from a named model on a named host), route "
    "(tier routing quick|interactive|quality or auto-triage, falling back down the tier's chain), "
    "embed (vector, default nomic-embed-text). An EMPTY response comes back with a warning: treat it "
    "as a failure, not an answer. Hosts are limited to the configured machines, localhost and "
    "private-network addresses."
)
CODE_TOOLS_NOTE = (
    " Code tools are enabled: read_file, write_file, edit_file, list_files, search_files and "
    "run_command act inside {} only. They work on files and commands directly, not through the "
    "local models."
)

TRIAGE_SYSTEM = (
    "You are a prompt classifier. Respond with ONLY one word.\n"
    "Classify the user's prompt into one of these categories:\n"
    "- quick: simple syntax, one-liners, short factual lookups, trivial fixes\n"
    "- interactive: medium tasks, explanations, moderate code, debugging help\n"
    "- quality: hard problems, architecture design, complex debugging, large code\n"
    "Respond with ONLY: quick, interactive, or quality"
)

# Used only when no config file is found. Same models as
# hivemind-config.example.json, all on this machine.
DEFAULT_CONFIG = {
    "machines": {
        "localhost": {"host": "127.0.0.1", "port": DEFAULT_PORT, "label": "Ollama on this machine"},
    },
    "triage": {"machine": "localhost", "model": "granite3-moe:1b", "default": "interactive"},
    "routes": {
        "quick": [{"machine": "localhost", "model": "qwen2.5-coder:3b"}],
        "interactive": [{"machine": "localhost", "model": "phi4:14b"},
                        {"machine": "localhost", "model": "qwen2.5-coder:7b"}],
        "quality": [{"machine": "localhost", "model": "gpt-oss:20b"}],
    },
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DIR = os.path.join(os.path.expanduser("~"), ".hivemind")
LOG_MAX_BYTES = 2 * 1024 * 1024


def _log_path() -> Optional[str]:
    v = os.environ.get("HIVEMIND_MCP_LOG", "").strip()
    if v.lower() in ("off", "none", "0", "false", "no"):
        return None
    return os.path.expanduser(v) if v else os.path.join(USER_DIR, "mcp_server.log")


LOG_PATH = _log_path()

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

STATE = {
    "initialized": False,
    "protocol_version": None,
    "client": None,
    "config": None,
    "config_path": None,
    "config_override": None,   # --config PATH
    "framing": "newline",      # or "content-length" if a client ever uses it
    "code_tools": None,        # dict once --code-tools was asked for
}


# ── Logging (never stdout) ───────────────────────────────────────────────────

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_DEBUG = os.environ.get("HIVEMIND_MCP_DEBUG", "") not in ("", "0", "false", "no")
_FILE_LEVEL = 10 if _DEBUG else 20
# Claude Code records everything a server writes to stderr as an error, so
# only warnings and errors go there; the log file has the rest.
_STDERR_LEVEL = 10 if _DEBUG else 30


def log(level: str, msg: str) -> None:
    n = _LEVELS.get(level, 20)
    line = "{} {:<7} {}".format(time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    if n >= _STDERR_LEVEL:
        try:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:
            pass
    if n >= _FILE_LEVEL and LOG_PATH:
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            try:
                if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                    os.replace(LOG_PATH, LOG_PATH + ".1")
            except OSError:
                pass
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ── Config ───────────────────────────────────────────────────────────────────

def config_candidates() -> List[Tuple[str, bool]]:
    """(path, explicit) in search order; explicit = named by --config or $HIVEMIND_CONFIG."""
    cands = []
    if STATE["config_override"]:
        cands.append((os.path.expanduser(STATE["config_override"]), True))
    env = os.environ.get("HIVEMIND_CONFIG", "").strip()
    if env:
        cands.append((os.path.expanduser(env), True))
    cands.append((os.path.join(SCRIPT_DIR, "hivemind-config.json"), False))
    cands.append((os.path.join(USER_DIR, "hivemind-config.json"), False))
    return cands


def load_config(force: bool = False,
                candidates: Optional[List[Tuple[str, bool]]] = None) -> Dict[str, Any]:
    if STATE["config"] is not None and not force:
        return STATE["config"]
    for path, explicit in (candidates if candidates is not None else config_candidates()):
        if not os.path.isfile(path):
            if explicit:
                log("WARNING", "config {} not found; trying the next location".format(path))
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            log("WARNING", "config {} unreadable ({}); trying the next location".format(path, e))
            continue
        if isinstance(cfg, dict) and isinstance(cfg.get("machines"), dict):
            STATE["config"], STATE["config_path"] = cfg, path
            log("INFO", "config loaded from {}".format(path))
            return cfg
        log("WARNING", "config {} has no 'machines' object; trying the next location".format(path))
    STATE["config"] = json.loads(json.dumps(DEFAULT_CONFIG))
    STATE["config_path"] = "(built-in default: localhost)"
    log("INFO", "no hivemind-config.json found; using the built-in default (localhost:11434)")
    return STATE["config"]


def set_config(cfg: Dict[str, Any], path: str = "(injected)") -> None:
    """For tests: inject a config instead of reading one from disk."""
    STATE["config"], STATE["config_path"] = cfg, path


def default_machine(cfg: Dict[str, Any]) -> str:
    """The `default_machine` key when it names a machine, else the first machine listed."""
    machines = cfg.get("machines", {}) or {}
    dm = cfg.get("default_machine")
    if dm in machines:
        return dm
    return next(iter(machines), "")


def tier_names(cfg: Dict[str, Any]) -> List[str]:
    return list((cfg.get("routes", {}) or {}).keys())


# ── Host resolution ──────────────────────────────────────────────────────────

class ToolError(Exception):
    """A plain, model-facing failure -> isError result."""


def machine_address(name: str, m: Any) -> Tuple[str, int]:
    host = m.get("host") if isinstance(m, dict) else None
    if not host:
        raise ToolError("machine '{}' has no 'host' in {}".format(name, STATE["config_path"]))
    try:
        port = int(m.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        raise ToolError("machine '{}' has a bad 'port' in {}".format(name, STATE["config_path"]))
    return str(host), port


def _is_private_address(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    h = host.lower()
    if h == "localhost" or "." not in h:      # bare names resolve on the local network
        return True
    return h.endswith((".local", ".lan", ".home", ".home.arpa", ".internal", ".localhost"))


def resolve_host(host: Optional[str], cfg: Dict[str, Any]) -> Tuple[str, str, int]:
    """-> (machine_name, host, port). Allowed: configured machines (by name or by
    host:port), localhost, and private-network addresses - so a tool call cannot
    send a prompt to an arbitrary internet host."""
    machines = cfg.get("machines", {}) or {}
    if host is None or str(host).strip() == "":
        name = default_machine(cfg)
        if not name:
            raise ToolError("no machines configured and no host given")
        return (name,) + machine_address(name, machines[name])
    h = str(host).strip()
    if h in machines:
        return (h,) + machine_address(h, machines[h])
    if h.lower() in ("localhost", "local"):
        return "localhost", "127.0.0.1", DEFAULT_PORT
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.rstrip("/")
    port = DEFAULT_PORT
    port_text = None
    if h.startswith("[") and "]" in h:            # [ipv6] or [ipv6]:port
        close = h.index("]")
        rest = h[close + 1:]
        h = h[1:close]
        if rest.startswith(":"):
            port_text = rest[1:]
        elif rest:
            raise ToolError("bad host '{}'".format(host))
    elif h.count(":") == 1:
        h, port_text = h.split(":", 1)
    if port_text is not None:
        try:
            port = int(port_text)
        except ValueError:
            raise ToolError("bad port in host '{}'".format(host))
    for name, m in machines.items():
        if isinstance(m, dict) and str(m.get("host", "")).lower() == h.lower():
            try:
                if int(m.get("port", DEFAULT_PORT)) == port:
                    return name, h, port
            except (TypeError, ValueError):
                pass
    if not _is_private_address(h):
        raise ToolError(
            "host '{}' is not a configured machine or a private-network address; "
            "use one of {} or a LAN IP[:port]".format(host, ", ".join(list(machines) + ["localhost"])))
    return h, h, port


# ── HTTP to Ollama (stdlib http.client; 5 s connect, separate read timeout) ──

class HostUnreachable(ToolError):
    pass


class GenerationTimeout(ToolError):
    pass


def ollama_request(method: str, host: str, port: int, path: str,
                   body: Optional[Dict[str, Any]] = None,
                   read_timeout: float = DEFAULT_GENERATE_TIMEOUT_S) -> Tuple[int, Any]:
    """-> (status, parsed JSON or text). Raises HostUnreachable / GenerationTimeout."""
    conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT_S)
    try:
        try:
            conn.connect()
        except (OSError, socket.timeout) as e:
            raise HostUnreachable("{}:{} unreachable ({})".format(host, port, e.__class__.__name__))
        if conn.sock is not None:
            conn.sock.settimeout(read_timeout)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except socket.timeout:
            raise GenerationTimeout("{}:{}{} timed out after {:.0f}s".format(host, port, path, read_timeout))
        except (OSError, http.client.HTTPException) as e:
            raise HostUnreachable("{}:{}{} failed ({}: {})".format(host, port, path, e.__class__.__name__, e))
        text = raw.decode("utf-8", errors="replace")
        try:
            return resp.status, json.loads(text) if text.strip() else {}
        except ValueError:
            return resp.status, text
    finally:
        try:
            conn.close()
        except Exception:
            pass


def ollama_error_text(status: int, payload: Any) -> str:
    if isinstance(payload, dict) and payload.get("error"):
        return "Ollama HTTP {}: {}".format(status, payload["error"])
    return "Ollama HTTP {}: {}".format(status, str(payload)[:300])


def get_tags(host: str, port: int) -> List[Dict[str, Any]]:
    status, payload = ollama_request("GET", host, port, "/api/tags", read_timeout=TAGS_TIMEOUT_S)
    if status != 200 or not isinstance(payload, dict):
        raise ToolError(ollama_error_text(status, payload))
    return payload.get("models", []) or []


def model_present(name: str, tags: List[Dict[str, Any]]) -> bool:
    names = {m.get("name", "") for m in tags}
    if name in names:
        return True
    if ":" not in name and (name + ":latest") in names:
        return True
    return False


def _gb(n: Any) -> Optional[float]:
    try:
        return round(float(n) / 1e9, 2)
    except (TypeError, ValueError):
        return None


def _ms(ns: Any) -> Optional[int]:
    try:
        return int(round(float(ns) / 1e6))
    except (TypeError, ValueError):
        return None


# ── Generation helpers ───────────────────────────────────────────────────────

def _clamp_float(v: Any, lo: float, hi: float, name: str) -> float:
    if isinstance(v, bool):
        raise ToolError("{} must be a number".format(name))
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ToolError("{} must be a number".format(name))
    return max(lo, min(hi, f))


def _clamp_int(v: Any, lo: int, hi: int, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ToolError("{} must be an integer".format(name))
    return max(lo, min(hi, int(v)))


def generate(machine: str, host: str, port: int, model: str, prompt: str,
             system: Optional[str], max_tokens: int, temperature: Optional[float],
             timeout_s: float) -> Dict[str, Any]:
    body = {"model": model, "prompt": prompt, "stream": False,
            "options": {"num_predict": max_tokens}}
    if system:
        body["system"] = system
    if temperature is not None:
        body["options"]["temperature"] = temperature
    t0 = time.time()
    status, payload = ollama_request("POST", host, port, "/api/generate", body, read_timeout=timeout_s)
    wall = time.time() - t0
    if status != 200 or not isinstance(payload, dict):
        msg = ollama_error_text(status, payload)
        if status == 404:
            msg += " (use list_models to see what this host has)"
        raise ToolError(msg)
    text = payload.get("response", "") or ""
    out = {
        "text": text,
        "model": payload.get("model", model),
        "machine": machine,
        "host": "{}:{}".format(host, port),
        "done_reason": payload.get("done_reason"),
        "prompt_tokens": payload.get("prompt_eval_count"),
        "output_tokens": payload.get("eval_count"),
        "eval_duration_ms": _ms(payload.get("eval_duration")),
        "total_duration_ms": _ms(payload.get("total_duration")),
        "load_duration_ms": _ms(payload.get("load_duration")),
        "wall_s": round(wall, 2),
    }
    ev, ed = payload.get("eval_count"), payload.get("eval_duration")
    if ev and ed:
        out["tokens_per_s"] = round(float(ev) / (float(ed) / 1e9), 1)
    thinking = payload.get("thinking")
    if thinking:
        out["thinking_preview"] = thinking[:THINKING_PREVIEW_CHARS]
        out["thinking_chars"] = len(thinking)
    if not text.strip():
        if thinking and out["done_reason"] == "length":
            out["warning"] = (
                "EMPTY RESPONSE: the model spent all {} max_tokens thinking. Raise max_tokens, "
                "or treat this as a failure.".format(max_tokens))
        else:
            out["warning"] = (
                "EMPTY RESPONSE: treat it as a failure, not an answer. A common cause is a model "
                "too large for the host's GPU (some Ollama builds fail silently on large MoE "
                "models, answering nothing within seconds); pick a smaller or dense model, or "
                "another host.")
    return out


def classify_tier(prompt: str, cfg: Dict[str, Any]) -> Tuple[str, str]:
    """hm's triage: -> (tier, how). Falls back to the config default."""
    triage = cfg.get("triage", {}) or {}
    default = triage.get("default", "interactive")
    routes = cfg.get("routes", {}) or {}
    if default not in routes and routes:
        default = next(iter(routes))
    mname = triage.get("machine")
    machine = (cfg.get("machines", {}) or {}).get(mname or "", {})
    if not machine:
        return default, "default (no triage machine configured)"
    model = triage.get("model", "granite3-moe:1b")
    body = {"model": model, "system": TRIAGE_SYSTEM, "prompt": prompt, "stream": False,
            "options": {"num_predict": 10, "temperature": 0.1}}
    try:
        host, port = machine_address(mname, machine)
        status, payload = ollama_request("POST", host, port, "/api/generate", body,
                                         read_timeout=TRIAGE_TIMEOUT_S)
        if status == 200 and isinstance(payload, dict):
            words = (payload.get("response", "") or "").strip().lower().split()
            if words:
                word = words[0].strip(".,!?;:'\"")
                if word in routes:
                    return word, "triage by {} on {}".format(model, mname)
            return default, "default (triage answered '{}')".format((payload.get("response", "") or "")[:40])
        return default, "default (triage HTTP {})".format(status)
    except ToolError as e:
        return default, "default ({})".format(e)


# ── Model tools ──────────────────────────────────────────────────────────────

def tool_list_models(args: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    name, host, port = resolve_host(args.get("host"), cfg)
    tags = get_tags(host, port)
    models = []
    for m in sorted(tags, key=lambda x: x.get("name", "")):
        det = m.get("details", {}) or {}
        models.append({
            "name": m.get("name"),
            "size_gb": _gb(m.get("size")),
            "parameters": det.get("parameter_size"),
            "quantization": det.get("quantization_level"),
            "family": det.get("family"),
            "modified_at": m.get("modified_at"),
        })
    return {"machine": name, "host": "{}:{}".format(host, port), "count": len(models), "models": models}


def quick_model_for(machine: str, cfg: Dict[str, Any]) -> str:
    quick = (cfg.get("routes", {}) or {}).get("quick", []) or []
    for t in quick:
        if t.get("machine") == machine and t.get("model"):
            return t["model"]
    if quick and quick[0].get("model"):
        return quick[0]["model"]
    return "qwen2.5-coder:3b"


def _generation_args(args: Dict[str, Any]) -> Tuple[Optional[str], int, Optional[float], float]:
    system = args.get("system")
    if system is not None and not isinstance(system, str):
        raise ToolError("'system' must be a string")
    max_tokens = _clamp_int(args.get("max_tokens", DEFAULT_MAX_TOKENS), 1, 32768, "max_tokens")
    temperature = args.get("temperature")
    if temperature is not None:
        temperature = _clamp_float(temperature, 0.0, 2.0, "temperature")
    timeout_s = _clamp_float(args.get("timeout_s", DEFAULT_GENERATE_TIMEOUT_S), 1.0,
                             MAX_GENERATE_TIMEOUT_S, "timeout_s")
    return system, max_tokens, temperature, timeout_s


def tool_ask_local(args: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolError("'prompt' (non-empty string) is required")
    name, host, port = resolve_host(args.get("host"), cfg)
    model = args.get("model") or quick_model_for(name, cfg)
    if not isinstance(model, str):
        raise ToolError("'model' must be a string")
    system, max_tokens, temperature, timeout_s = _generation_args(args)
    out = generate(name, host, port, model, prompt, system, max_tokens, temperature, timeout_s)
    out["advisory"] = ADVISORY
    return out


def tool_route(args: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolError("'prompt' (non-empty string) is required")
    routes = cfg.get("routes", {}) or {}
    if not routes:
        raise ToolError("no routes configured")
    tier = args.get("tier") or "auto"
    if not isinstance(tier, str):
        raise ToolError("'tier' must be a string")
    how = "requested"
    if tier == "auto":
        tier, how = classify_tier(prompt, cfg)
    if tier not in routes:
        raise ToolError("unknown tier '{}'; tiers: auto, {}".format(tier, ", ".join(routes)))
    system, max_tokens, temperature, timeout_s = _generation_args(args)

    machines = cfg.get("machines", {}) or {}
    tried = []
    for target in routes[tier] or []:
        mname, model = target.get("machine"), target.get("model")
        machine = machines.get(mname or "", {})
        if not machine or not model:
            tried.append({"machine": mname, "model": model, "result": "skipped: not configured"})
            continue
        try:
            host, port = machine_address(mname, machine)
            tags = get_tags(host, port)
        except ToolError as e:
            tried.append({"machine": mname, "model": model, "result": "unreachable: {}".format(e)})
            continue
        if not model_present(model, tags):
            tried.append({"machine": mname, "model": model, "result": "model not installed on host"})
            continue
        try:
            out = generate(mname, host, port, model, prompt, system, max_tokens, temperature, timeout_s)
        except HostUnreachable as e:
            tried.append({"machine": mname, "model": model, "result": "failed: {}".format(e)})
            continue
        except GenerationTimeout as e:
            tried.append({"machine": mname, "model": model, "result": "timeout: {}".format(e)})
            raise ToolError("{} (tier {}; tried: {})".format(e, tier, json.dumps(tried)))
        tried.append({"machine": mname, "model": model, "result": "answered"})
        out["tier"] = tier
        out["tier_chosen_by"] = how
        out["fallback_path"] = tried
        out["advisory"] = ADVISORY
        return out
    raise ToolError("no target for tier '{}' answered; tried: {}".format(tier, json.dumps(tried)))


def tool_embed(args: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolError("'text' (non-empty string) is required")
    model = args.get("model") or DEFAULT_EMBED_MODEL
    if not isinstance(model, str):
        raise ToolError("'model' must be a string")
    name, host, port = resolve_host(args.get("host"), cfg)
    digits = _clamp_int(args.get("round", 6), 0, 12, "round")
    include_vector = args.get("include_vector", True)
    if not isinstance(include_vector, bool):
        raise ToolError("'include_vector' must be a boolean")
    t0 = time.time()
    status, payload = ollama_request("POST", host, port, "/api/embed",
                                     {"model": model, "input": text},
                                     read_timeout=DEFAULT_GENERATE_TIMEOUT_S)
    endpoint = "/api/embed"
    vector = None
    if status == 200 and isinstance(payload, dict) and payload.get("embeddings"):
        vector = payload["embeddings"][0]
    elif status == 404 and not (isinstance(payload, dict) and "not found" in str(payload.get("error", "")).lower()):
        # an older Ollama without /api/embed -> the legacy endpoint
        endpoint = "/api/embeddings"
        status, payload = ollama_request("POST", host, port, endpoint,
                                         {"model": model, "prompt": text},
                                         read_timeout=DEFAULT_GENERATE_TIMEOUT_S)
        if status == 200 and isinstance(payload, dict) and payload.get("embedding"):
            vector = payload["embedding"]
    if vector is None:
        msg = ollama_error_text(status, payload)
        if status == 404:
            msg += " (use list_models to see what this host has)"
        raise ToolError(msg)
    out = {
        "model": payload.get("model", model) if isinstance(payload, dict) else model,
        "machine": name,
        "host": "{}:{}".format(host, port),
        "endpoint": endpoint,
        "dimension": len(vector),
        "input_chars": len(text),
        "prompt_tokens": payload.get("prompt_eval_count") if isinstance(payload, dict) else None,
        "wall_s": round(time.time() - t0, 2),
    }
    if include_vector:
        out["vector"] = [round(float(v), digits) for v in vector]
    return out


def code_tools_status() -> Dict[str, Any]:
    ct = STATE["code_tools"]
    if not ct:
        return {"enabled": False}
    out = {"enabled": ct["enabled"], "project_dir": ct["project_dir"]}
    if ct.get("reason"):
        out["reason"] = ct["reason"]
    return out


def tool_health(args: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    hosts = []
    for name, m in (cfg.get("machines", {}) or {}).items():
        label = m.get("label") if isinstance(m, dict) else None
        try:
            host, port = machine_address(name, m)
        except ToolError as e:
            hosts.append({"machine": name, "label": label, "reachable": False, "error": str(e)})
            continue
        entry = {"machine": name, "host": "{}:{}".format(host, port), "label": label}
        t0 = time.time()
        try:
            tags = get_tags(host, port)
            entry["reachable"] = True
            entry["latency_ms"] = int((time.time() - t0) * 1000)
            entry["model_count"] = len(tags)
        except ToolError as e:
            entry["reachable"] = False
            entry["error"] = str(e)
            hosts.append(entry)
            continue
        try:
            st, v = ollama_request("GET", host, port, "/api/version", read_timeout=TAGS_TIMEOUT_S)
            if st == 200 and isinstance(v, dict):
                entry["ollama_version"] = v.get("version")
        except ToolError:
            pass
        try:
            st, ps = ollama_request("GET", host, port, "/api/ps", read_timeout=TAGS_TIMEOUT_S)
            if st == 200 and isinstance(ps, dict):
                entry["loaded"] = [{
                    "model": x.get("name"),
                    "size_gb": _gb(x.get("size")),
                    "vram_gb": _gb(x.get("size_vram")),
                    "expires_at": x.get("expires_at"),
                } for x in ps.get("models", []) or []]
        except ToolError:
            pass
        hosts.append(entry)
    return {
        "hosts": hosts,
        "default_machine": default_machine(cfg),
        "tiers": {t: ["{}:{}".format(x.get("machine"), x.get("model")) for x in (targets or [])]
                  for t, targets in (cfg.get("routes", {}) or {}).items()},
        "triage": cfg.get("triage"),
        "config_path": STATE["config_path"],
        "log_path": LOG_PATH or "(off)",
        "code_tools": code_tools_status(),
        "server": "{} {}".format(SERVER_NAME, SERVER_VERSION),
        "protocol_version": STATE["protocol_version"],
    }


TOOL_HANDLERS = {
    "health": tool_health,
    "list_models": tool_list_models,
    "ask_local": tool_ask_local,
    "route": tool_route,
    "embed": tool_embed,
}


# ── Code tools (optional): hmc's six tools from hivemind-code.py ─────────────

CODE_TOOL_NAMES = ("read_file", "write_file", "edit_file", "list_files", "search_files", "run_command")
CODE_TOOLS_THAT_CHANGE = ("write_file", "edit_file", "run_command")
# The prefixes hmc's tool functions use for failures.
HMC_ERROR_PREFIXES = ("Error: ", "Error reading '", "Error writing '", "Error in tool '",
                      "Error in glob: ", "Error running command: ", "Cancelled: ")


class _NoStdinSubprocess:
    """hmc's run_command calls subprocess.run without stdin=, so the command
    would inherit this server's stdin, which is the MCP protocol stream: a
    command that reads input would swallow the client's messages or hang until
    the timeout. The loaded hmc module gets this stand-in, whose run() gives
    every command an empty stdin. Everything else is the real module."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("stdin", self._real.DEVNULL)
        return self._real.run(*args, **kwargs)


def _load_hmc() -> Any:
    path = os.path.join(SCRIPT_DIR, "hivemind-code.py")
    if not os.path.isfile(path):
        raise ImportError("hivemind-code.py not found next to hivemind_mcp.py")
    spec = importlib.util.spec_from_file_location("hivemind_code_for_mcp", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load {}".format(path))
    mod = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(mod)
    mod.subprocess = _NoStdinSubprocess(mod.subprocess)
    return mod


def enable_code_tools(project_dir: Optional[str] = None) -> Dict[str, Any]:
    """Turn on hmc's tools in project_dir (default: $CLAUDE_PROJECT_DIR, which
    Claude Code sets for stdio servers, else the current directory). A missing
    directory, a filesystem root or the home folder leaves them off, with the
    reason in `health`."""
    raw = project_dir or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    pd = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
    state = {"enabled": False, "project_dir": pd, "module": None, "reason": None}
    STATE["code_tools"] = state
    home = os.path.realpath(os.path.expanduser("~"))
    if not os.path.isdir(pd):
        state["reason"] = "project directory {} does not exist".format(pd)
    elif os.path.dirname(pd) == pd:
        state["reason"] = "project directory {} is a filesystem root; pass --project-dir".format(pd)
    elif os.path.normcase(pd) == os.path.normcase(home):
        state["reason"] = "project directory {} is the home folder; pass --project-dir".format(pd)
    else:
        try:
            state["module"] = _load_hmc()
            state["enabled"] = True
        except Exception as e:
            state["reason"] = "could not load hivemind-code.py ({}: {})".format(e.__class__.__name__, e)
    if state["enabled"]:
        log("INFO", "code tools enabled in {}".format(pd))
    else:
        log("WARNING", "code tools NOT enabled: {}".format(state["reason"]))
    return state


def disable_code_tools() -> None:
    STATE["code_tools"] = None


def code_tools_enabled() -> bool:
    ct = STATE["code_tools"]
    return bool(ct and ct.get("enabled"))


def code_tool_definitions() -> List[Dict[str, Any]]:
    if not code_tools_enabled():
        return []
    ct = STATE["code_tools"]
    out = []
    for t in ct["module"].TOOLS:
        fn = t["function"]
        name = fn["name"]
        if name not in CODE_TOOL_NAMES:
            continue
        changes = name in CODE_TOOLS_THAT_CHANGE
        d = {
            "name": name,
            "title": "hmc " + name.replace("_", " "),
            "description": "{} Acts inside {} only (hmc's path containment{}).".format(
                fn["description"], ct["project_dir"],
                " and blocklist of destructive commands" if name == "run_command" else ""),
            "inputSchema": fn["parameters"],
            "annotations": {"readOnlyHint": not changes, "destructiveHint": changes,
                            "idempotentHint": name in ("read_file", "list_files", "search_files", "write_file"),
                            "openWorldHint": name == "run_command"},
        }
        if changes:
            # Claude Code then asks on every call, even when an allow rule
            # matches the server or the session runs in auto mode.
            d["_meta"] = {"anthropic/requiresUserInteraction": True}
        out.append(d)
    return out


def call_code_tool(name: str, args: Dict[str, Any]) -> Tuple[str, bool]:
    ct = STATE["code_tools"]
    buf = io.StringIO()
    # In trust mode hmc prints "[auto-approved] ..." to stdout, which here is
    # the protocol stream; capture it for the log instead.
    with contextlib.redirect_stdout(buf):
        result = ct["module"].execute_tool(name, args, ct["project_dir"], trust_mode=True)
    printed = buf.getvalue().strip()
    if printed:
        log("INFO", "{}: {}".format(name, printed.replace("\n", " | ")))
    text = str(result)
    return text, text.startswith(HMC_ERROR_PREFIXES)


# ── Tool definitions ─────────────────────────────────────────────────────────

def _host_schema(cfg: Dict[str, Any]) -> Dict[str, Any]:
    names = list((cfg.get("machines", {}) or {}).keys())
    return {"type": "string",
            "description": ("Which Ollama host: a machine name from hivemind-config.json ({}; default {}), "
                            "'localhost', or a private-network address IP[:port]. Other hosts are refused."
                            ).format(", ".join(names) or "none configured", default_machine(cfg) or "none")}


def _gen_props() -> Dict[str, Any]:
    return {
        "system": {"type": "string", "description": "Optional system prompt."},
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 32768,
                       "description": "Max tokens to generate (Ollama num_predict). Default 1024."},
        "temperature": {"type": "number", "minimum": 0, "maximum": 2,
                        "description": "Sampling temperature; omit for the model default."},
        "timeout_s": {"type": "number", "minimum": 1, "maximum": MAX_GENERATE_TIMEOUT_S,
                      "description": "Generation timeout in seconds (default 120, max 900; connecting "
                                     "always times out after 5 s). A large model only partly offloaded "
                                     "to the GPU can need 300 or more."},
    }


def tool_definitions(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    cfg = cfg or load_config()
    tiers = tier_names(cfg) or ["quick", "interactive", "quality"]
    triage_model = (cfg.get("triage", {}) or {}).get("model", "granite3-moe:1b")
    ro = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
    ask_props = {"prompt": {"type": "string", "description": "The prompt (user turn)."},
                 "model": {"type": "string",
                           "description": "Ollama model tag, e.g. qwen2.5-coder:7b. Default: the host's "
                                          "model in the quick tier of hivemind-config.json."},
                 "host": _host_schema(cfg)}
    ask_props.update(_gen_props())
    route_props = {"prompt": {"type": "string", "description": "The prompt (user turn)."},
                   "tier": {"type": "string", "enum": ["auto"] + tiers,
                            "description": "Routing tier. 'auto' (default) lets the triage model ({}) "
                                           "classify the prompt; or force one of: {}.".format(
                                               triage_model, ", ".join(tiers))}}
    route_props.update(_gen_props())
    return [
        {
            "name": "health",
            "title": "Ollama host health",
            "description": "Which configured Ollama hosts answer (latency, Ollama version, model count, "
                           "models currently loaded in memory), plus the routing tiers and which config "
                           "file the server uses. Call it first when another tool fails.",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": dict(ro, idempotentHint=True),
        },
        {
            "name": "list_models",
            "title": "List local models",
            "description": "Models installed on one Ollama host with size (GB), parameter count, "
                           "quantization and family. Default: the default machine of hivemind-config.json.",
            "inputSchema": {"type": "object", "properties": {"host": _host_schema(cfg)}},
            "annotations": dict(ro, idempotentHint=True),
        },
        {
            "name": "ask_local",
            "title": "Ask a local model",
            "description": "One non-streaming completion from a named local model (Ollama /api/generate). "
                           "Returns the text plus model, machine, host, token counts and durations. "
                           "ADVISORY ONLY: local-model output is a draft to verify, never a gate or a judge. "
                           "An empty response is flagged with a warning and is a failure, not an answer.",
            "inputSchema": {"type": "object", "properties": ask_props, "required": ["prompt"]},
            "annotations": ro,
        },
        {
            "name": "route",
            "title": "Route through the hivemind",
            "description": "hm's routing: picks the machine and model for a tier (" + ", ".join(tiers) +
                           ", or auto-triage) from hivemind-config.json, falls down the tier's chain when "
                           "a host does not answer or lacks the model, runs the prompt and reports which "
                           "model on which machine answered and the fallback path taken. ADVISORY ONLY "
                           "(see ask_local).",
            "inputSchema": {"type": "object", "properties": route_props, "required": ["prompt"]},
            "annotations": ro,
        },
        {
            "name": "embed",
            "title": "Embed text",
            "description": "Embedding vector for one text via Ollama /api/embed (falls back to "
                           "/api/embeddings on older Ollama). Default model nomic-embed-text (768 "
                           "dimensions). Returns dimension, token count and the vector (rounded; set "
                           "include_vector=false for the metadata only).",
            "inputSchema": {"type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "Text to embed."},
                                "model": {"type": "string", "description": "Embedding model; default nomic-embed-text."},
                                "host": _host_schema(cfg),
                                "round": {"type": "integer", "minimum": 0, "maximum": 12,
                                          "description": "Decimal places for the vector values (default 6)."},
                                "include_vector": {"type": "boolean",
                                                   "description": "Return the vector itself (default true)."}},
                            "required": ["text"]},
            "annotations": dict(ro, idempotentHint=True),
        },
    ]


# ── JSON-RPC ─────────────────────────────────────────────────────────────────

def _result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


COMPACT_TOOLS = {"embed"}   # a 768-number vector is unreadable one value per line


def _text_result(obj: Any, is_error: bool = False, compact: bool = False) -> Dict[str, Any]:
    if isinstance(obj, str):
        text = obj
    elif compact:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(obj, indent=2, ensure_ascii=False)
    res = {"content": [{"type": "text", "text": text}]}
    if is_error:
        res["isError"] = True
    return res


def negotiate_version(requested: Any) -> str:
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return FALLBACK_PROTOCOL_VERSION


def handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    requested = params.get("protocolVersion")
    version = negotiate_version(requested)
    STATE["protocol_version"] = version
    STATE["client"] = params.get("clientInfo")
    log("INFO", "initialize from {} requested {} -> {}".format(
        json.dumps(params.get("clientInfo")), requested, version))
    load_config()
    instructions = INSTRUCTIONS
    if code_tools_enabled():
        instructions += CODE_TOOLS_NOTE.format(STATE["code_tools"]["project_dir"])
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                       "title": "Hivemind (local Ollama models)"},
        "instructions": instructions,
    }


def handle_tools_call(req_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    name = params.get("name")
    is_code = code_tools_enabled() and name in CODE_TOOL_NAMES
    if not isinstance(name, str) or (name not in TOOL_HANDLERS and not is_code):
        return _error(req_id, INVALID_PARAMS, "Unknown tool: {}".format(name))
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _result(req_id, _text_result("arguments must be a JSON object", is_error=True))
    t0 = time.time()
    try:
        if is_code:
            text, failed = call_code_tool(name, args)
            log("INFO", "tools/call {} {} in {:.1f}s".format(name, "failed" if failed else "ok", time.time() - t0))
            return _result(req_id, _text_result(text, is_error=failed))
        out = TOOL_HANDLERS[name](args)
        log("INFO", "tools/call {} ok in {:.1f}s".format(name, time.time() - t0))
        return _result(req_id, _text_result(out, compact=name in COMPACT_TOOLS))
    except ToolError as e:
        log("WARNING", "tools/call {} failed: {}".format(name, e))
        return _result(req_id, _text_result("{} failed: {}".format(name, e), is_error=True))
    except Exception as e:  # never let a bug kill the server
        log("ERROR", "tools/call {} crashed: {}\n{}".format(name, e, traceback.format_exc()))
        return _result(req_id, _text_result("{} crashed: {}: {}".format(name, e.__class__.__name__, e),
                                            is_error=True))


def handle_one(msg: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(msg, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request: not a JSON object")
    if msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id"), INVALID_REQUEST, "Invalid Request: jsonrpc must be '2.0'")
    method = msg.get("method")
    if method is None:
        # a response to a server->client request; this server never sends any
        return None
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        if "id" in msg:
            return _error(req_id, INVALID_PARAMS, "params must be an object")
        return None
    if "id" not in msg:   # a notification: never answered
        if method == "notifications/initialized":
            STATE["initialized"] = True
            log("INFO", "client initialized")
        elif method == "notifications/cancelled":
            log("INFO", "cancel requested for {}".format(params.get("requestId")))
        else:
            log("DEBUG", "ignoring notification {}".format(method))
        return None
    try:
        if method == "initialize":
            return _result(req_id, handle_initialize(params))
        if method == "ping":
            return _result(req_id, {})
        if method == "tools/list":
            return _result(req_id, {"tools": tool_definitions() + code_tool_definitions()})
        if method == "tools/call":
            return handle_tools_call(req_id, params)
        log("DEBUG", "method not found: {}".format(method))
        return _error(req_id, METHOD_NOT_FOUND, "Method not found: {}".format(method))
    except Exception as e:
        log("ERROR", "{} crashed: {}\n{}".format(method, e, traceback.format_exc()))
        return _error(req_id, INTERNAL_ERROR, "Internal error: {}: {}".format(e.__class__.__name__, e))


def handle(request: Any) -> Any:
    """Process one parsed JSON-RPC message (or a batch list). Returns the response
    dict, a list of responses for a batch, or None when nothing is to be sent."""
    if isinstance(request, list):
        if not request:
            return _error(None, INVALID_REQUEST, "Invalid Request: empty batch")
        out = [r for r in (handle_one(m) for m in request) if r is not None]
        return out or None
    return handle_one(request)


def handle_text(line: str) -> Optional[str]:
    """Parse one raw message, handle it, return the encoded response (or None)."""
    try:
        msg = json.loads(line)
    except ValueError as e:
        return json.dumps(_error(None, PARSE_ERROR, "Parse error: {}".format(e)))
    resp = handle(msg)
    if resp is None:
        return None
    return json.dumps(resp, ensure_ascii=True)


# ── stdio loop ───────────────────────────────────────────────────────────────

def _write_out(stream: Any, text: str) -> None:
    data = text.encode("utf-8")
    if STATE["framing"] == "content-length":
        stream.write(b"Content-Length: " + str(len(data)).encode("ascii") + b"\r\n\r\n")
    stream.write(data + b"\n")
    stream.flush()


def _read_message(stdin: Any) -> Optional[bytes]:
    """Next raw message, newline-delimited (tolerates Content-Length framing)."""
    while True:
        line = stdin.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            try:
                length = int(stripped.split(b":", 1)[1].strip())
            except ValueError:
                continue
            while True:  # remaining headers up to the blank line
                h = stdin.readline()
                if not h or not h.strip():
                    break
            STATE["framing"] = "content-length"
            body = b""
            while len(body) < length:
                chunk = stdin.read(length - len(body))
                if not chunk:
                    return None
                body += chunk
            return body
        return stripped


def serve_stdio() -> int:
    log("INFO", "{} {} starting (pid {}, python {} at {}, script {}, log {})".format(
        SERVER_NAME, SERVER_VERSION, os.getpid(), sys.version.split()[0], sys.executable,
        os.path.abspath(__file__), LOG_PATH or "off"))
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer   # the protocol stream, held even if sys.stdout is redirected
    try:
        while True:
            raw = _read_message(stdin)
            if raw is None:
                break
            text = raw.decode("utf-8", errors="replace")
            try:
                out = handle_text(text)
            except Exception as e:
                log("ERROR", "unhandled: {}\n{}".format(e, traceback.format_exc()))
                out = json.dumps(_error(None, INTERNAL_ERROR, "Internal error: {}".format(e)))
            if out is not None:
                try:
                    _write_out(stdout, out)
                except (BrokenPipeError, OSError):
                    log("INFO", "stdout closed; exiting")
                    return 0
    except KeyboardInterrupt:
        # Claude Code stops stdio servers with SIGINT (then SIGTERM, then SIGKILL).
        log("INFO", "interrupted; exiting")
        return 0
    log("INFO", "stdin closed; exiting")
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="hivemind_mcp.py",
        description="hivemind MCP server (stdio). Run with no options from an MCP client.")
    parser.add_argument("--config", metavar="PATH",
                        help="hivemind-config.json to use (default: $HIVEMIND_CONFIG, the file next to "
                             "this script, ~/.hivemind/hivemind-config.json, then localhost)")
    parser.add_argument("--code-tools", action="store_true",
                        help="also serve hmc's six file and command tools (off by default)")
    parser.add_argument("--project-dir", metavar="DIR",
                        help="directory the code tools act in (default: $CLAUDE_PROJECT_DIR, else the "
                             "current directory)")
    parser.add_argument("--self-test", action="store_true",
                        help="offline check: handshake and tool list, printed to stderr; then exit")
    parser.add_argument("--health", action="store_true",
                        help="probe the configured hosts, print the result to stderr; then exit")
    args = parser.parse_args(argv)
    if args.project_dir and not args.code_tools:
        parser.error("--project-dir only applies with --code-tools")
    if args.config:
        STATE["config_override"] = args.config
    if args.code_tools:
        enable_code_tools(args.project_dir)
    if args.self_test:
        # offline sanity: handshake + tools/list without touching the network
        init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "self-test", "version": "0"}}})["result"]
        tools = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        ct = code_tools_status()
        lines = [
            "initialize: protocol {}, server {} {}".format(
                init["protocolVersion"], init["serverInfo"]["name"], init["serverInfo"]["version"]),
            "tools: " + ", ".join(t["name"] for t in tools),
            "config: " + str(STATE["config_path"]),
            "default host: {}".format(default_machine(load_config()) or "(none)"),
            "code tools: " + ("on in {}".format(ct["project_dir"]) if ct["enabled"]
                              else "off" + (" ({})".format(ct["reason"]) if ct.get("reason") else "")),
            "log: " + (LOG_PATH or "off"),
        ]
        sys.stderr.write("\n".join(lines) + "\n")
        return 0
    if args.health:
        sys.stderr.write(json.dumps(tool_health({}), indent=2) + "\n")
        return 0
    return serve_stdio()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

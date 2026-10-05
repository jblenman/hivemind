#!/usr/bin/env python3
"""
Tests for hivemind_mcp.py, the hivemind MCP server.

    python3 tests/test_mcp_server.py          # Windows: py tests/test_mcp_server.py
    python3 -m unittest discover -s tests     # the same tests

Offline tests always run; the only connection they open is a refused one to
127.0.0.1:9. Live tests talk to one Ollama and are skipped when it does not
answer:

    HIVEMIND_TEST_OLLAMA  host[:port] of the Ollama to test against (default:
                          the server's default machine - localhost:11434 unless
                          your hivemind-config.json says otherwise)
    HIVEMIND_TEST_MODEL   small generation model for the live tests (default
                          granite3-moe:1b; the tests that need it skip without it)

The embedding test needs nomic-embed-text on that host. The tests keep their
log in a temporary folder, never in ~/.hivemind. Stdlib unittest only.
"""

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "hivemind_mcp.py")
EXAMPLE_CONFIG = os.path.join(ROOT, "hivemind-config.example.json")

TMP = tempfile.mkdtemp(prefix="hivemind-mcp-tests-")
atexit.register(shutil.rmtree, TMP, True)
os.environ["HIVEMIND_MCP_LOG"] = os.path.join(TMP, "mcp_server.log")   # read when the module loads

sys.path.insert(0, ROOT)
import hivemind_mcp as S  # noqa: E402

TINY_MODEL = os.environ.get("HIVEMIND_TEST_MODEL", "").strip() or "granite3-moe:1b"
DEAD = {"host": "127.0.0.1", "port": 9}       # connection refused at once
FIVE = ["health", "list_models", "ask_local", "route", "embed"]

# Offline tests use this config, never whatever is on disk.
TEST_CONFIG = {
    "machines": {
        "box": {"host": "10.0.0.7", "port": 11434, "label": "test box"},
        "spare": {"host": "10.0.0.8", "port": 11435, "label": "second test box"},
    },
    "triage": {"machine": "box", "model": "granite3-moe:1b", "default": "interactive"},
    "routes": {
        "quick": [{"machine": "box", "model": "qwen2.5-coder:3b"}],
        "interactive": [{"machine": "box", "model": "phi4:14b"}],
        "quality": [{"machine": "spare", "model": "gpt-oss:20b"}],
    },
}
TEST_CONFIG_FILE = os.path.join(TMP, "test-config.json")
with open(TEST_CONFIG_FILE, "w", encoding="utf-8") as _f:
    json.dump(TEST_CONFIG, _f)


def use_test_config():
    S.set_config(json.loads(json.dumps(TEST_CONFIG)), "(test config)")


def rpc(method, params=None, _id=[100]):
    _id[0] += 1
    msg = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def call(name, args):
    return S.handle(rpc("tools/call", {"name": name, "arguments": args}))


def text_of(resp):
    return resp["result"]["content"][0]["text"]


def is_error(resp):
    return bool(resp.get("result", {}).get("isError"))


def server_env():
    env = dict(os.environ)
    env["HIVEMIND_CONFIG"] = TEST_CONFIG_FILE
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env


def initialize_msg(_id=1):
    return {"jsonrpc": "2.0", "id": _id, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}}


# ── The live host ────────────────────────────────────────────────────────────

def _live_target():
    spec = os.environ.get("HIVEMIND_TEST_OLLAMA", "").strip()
    if spec:
        if "://" in spec:
            spec = spec.split("://", 1)[1]
        spec = spec.rstrip("/")
        host, port = spec, 11434
        if spec.count(":") == 1:
            host, p = spec.split(":", 1)
            port = int(p)
        return {"host": host, "port": port, "label": "live test host"}
    try:
        cfg = S.load_config(force=True)
        name = S.default_machine(cfg)
        host, port = S.machine_address(name, cfg["machines"][name])
    except (S.ToolError, KeyError):
        host, port = "127.0.0.1", 11434
    return {"host": host, "port": port, "label": "live test host"}


LIVE = _live_target()


def _live_models():
    try:
        return {m.get("name") for m in S.get_tags(LIVE["host"], int(LIVE["port"]))}
    except S.ToolError:
        return None


LIVE_MODELS = _live_models()
needs_live = unittest.skipIf(LIVE_MODELS is None, "no Ollama answers at {}:{}".format(LIVE["host"], LIVE["port"]))


def live_config(routes=None):
    tiny = [{"machine": "live", "model": TINY_MODEL}]
    return {
        "machines": {"live": dict(LIVE)},
        "default_machine": "live",
        "triage": {"machine": "live", "model": TINY_MODEL, "default": "interactive"},
        "routes": routes or {"quick": tiny, "interactive": tiny, "quality": tiny},
    }


def setUpModule():
    use_test_config()


# ── Protocol ─────────────────────────────────────────────────────────────────

class ProtocolTests(unittest.TestCase):

    def test_initialize_echoes_each_supported_version(self):
        for v in S.SUPPORTED_PROTOCOL_VERSIONS:
            r = S.handle(rpc("initialize", {"protocolVersion": v, "capabilities": {},
                                            "clientInfo": {"name": "t", "version": "0"}}))
            self.assertEqual(r["result"]["protocolVersion"], v)
            self.assertIn("tools", r["result"]["capabilities"])
            self.assertEqual(r["result"]["serverInfo"]["name"], "hivemind")
            self.assertIn("never use a local model as a gate", r["result"]["instructions"])

    def test_initialize_unknown_version_gets_fallback(self):
        for v in ("1999-01-01", None, 42):
            r = S.handle(rpc("initialize", {"protocolVersion": v, "capabilities": {}}))
            self.assertEqual(r["result"]["protocolVersion"], S.FALLBACK_PROTOCOL_VERSION)

    def test_initialized_notification_has_no_response(self):
        self.assertIsNone(S.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertTrue(S.STATE["initialized"])
        self.assertIsNone(S.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                    "params": {"requestId": 5}}))
        self.assertIsNone(S.handle({"jsonrpc": "2.0", "method": "notifications/whatever"}))

    def test_ping(self):
        r = S.handle(rpc("ping"))
        self.assertEqual(r["result"], {})

    def test_unknown_method_is_32601(self):
        # server/discover: Claude Code 2.1.289 probes it before initialize
        for m in ("resources/list", "prompts/list", "completion/complete", "server/discover", "bogus"):
            r = S.handle(rpc(m))
            self.assertEqual(r["error"]["code"], -32601, m)
            self.assertNotIn("result", r)

    def test_invalid_request(self):
        self.assertEqual(S.handle("nope")["error"]["code"], -32600)
        self.assertEqual(S.handle({"id": 1, "method": "ping"})["error"]["code"], -32600)
        self.assertEqual(S.handle([])["error"]["code"], -32600)

    def test_parse_error(self):
        r = json.loads(S.handle_text("{not json"))
        self.assertEqual(r["error"]["code"], -32700)
        self.assertIsNone(r["id"])

    def test_batch(self):
        out = S.handle([rpc("ping"), {"jsonrpc": "2.0", "method": "notifications/initialized"}, rpc("bogus")])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["result"], {})
        self.assertEqual(out[1]["error"]["code"], -32601)

    def test_response_message_from_client_is_ignored(self):
        self.assertIsNone(S.handle({"jsonrpc": "2.0", "id": 7, "result": {}}))


# ── tools/list ───────────────────────────────────────────────────────────────

class ToolsListTests(unittest.TestCase):

    def test_tools_list_schema_sanity(self):
        tools = S.handle(rpc("tools/list"))["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(names, FIVE)
        for t in tools:
            self.assertTrue(t["description"].strip(), t["name"])
            self.assertEqual(t["inputSchema"]["type"], "object", t["name"])
            self.assertIn("properties", t["inputSchema"], t["name"])
            self.assertIn("annotations", t)
            self.assertTrue(t["annotations"]["readOnlyHint"])
            json.dumps(t)  # serialisable
        by = {t["name"]: t for t in tools}
        self.assertEqual(by["ask_local"]["inputSchema"]["required"], ["prompt"])
        self.assertEqual(by["route"]["inputSchema"]["required"], ["prompt"])
        self.assertEqual(by["embed"]["inputSchema"]["required"], ["text"])
        tiers = by["route"]["inputSchema"]["properties"]["tier"]["enum"]
        self.assertEqual(tiers[0], "auto")
        for tier in S.tier_names(S.load_config()):
            self.assertIn(tier, tiers)
        for p in ("prompt", "model", "system", "host", "max_tokens", "temperature", "timeout_s"):
            self.assertIn(p, by["ask_local"]["inputSchema"]["properties"])
        self.assertIn("ADVISORY", by["ask_local"]["description"])
        host_doc = by["list_models"]["inputSchema"]["properties"]["host"]["description"]
        self.assertIn("box, spare", host_doc)      # configured names are listed for the model


# ── Error paths (offline) ────────────────────────────────────────────────────

class ErrorPathTests(unittest.TestCase):

    def tearDown(self):
        use_test_config()

    def test_unknown_tool_is_jsonrpc_invalid_params(self):
        r = call("nope", {})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("Unknown tool", r["error"]["message"])

    def test_arguments_must_be_object(self):
        r = S.handle(rpc("tools/call", {"name": "health", "arguments": [1, 2]}))
        self.assertTrue(is_error(r))

    def test_missing_or_bad_params(self):
        cases = [
            ("ask_local", {}, "prompt"),
            ("ask_local", {"prompt": "   "}, "prompt"),
            ("ask_local", {"prompt": "hi", "max_tokens": "ten"}, "max_tokens"),
            ("ask_local", {"prompt": "hi", "temperature": "hot"}, "temperature"),
            ("ask_local", {"prompt": "hi", "system": 5}, "system"),
            ("route", {}, "prompt"),
            ("route", {"prompt": "hi", "tier": "turbo"}, "unknown tier"),
            ("embed", {}, "text"),
            ("embed", {"text": "x", "include_vector": "yes"}, "include_vector"),
        ]
        for name, args, needle in cases:
            r = call(name, args)
            self.assertTrue(is_error(r), (name, args))
            self.assertIn(needle, text_of(r), (name, args))

    def test_public_host_refused(self):
        for host in ("example.com", "http://ollama.example.org:11434", "8.8.8.8"):
            r = call("list_models", {"host": host})
            self.assertTrue(is_error(r), host)
            self.assertIn("private-network", text_of(r), host)

    def test_private_hosts_resolve(self):
        cfg = S.load_config()
        self.assertEqual(S.resolve_host(None, cfg), ("box", "10.0.0.7", 11434))
        self.assertEqual(S.resolve_host("spare", cfg), ("spare", "10.0.0.8", 11435))
        self.assertEqual(S.resolve_host("localhost", cfg), ("localhost", "127.0.0.1", 11434))
        self.assertEqual(S.resolve_host("10.0.0.99:11435", cfg), ("10.0.0.99", "10.0.0.99", 11435))
        self.assertEqual(S.resolve_host("http://172.16.5.9", cfg)[2], 11434)
        self.assertEqual(S.resolve_host("http://10.0.0.8:11435/", cfg)[0], "spare")   # configured host:port

    def test_unreachable_host(self):
        t0 = time.time()
        r = call("list_models", {"host": "127.0.0.1:9"})
        self.assertTrue(is_error(r))
        self.assertIn("unreachable", text_of(r))
        self.assertLess(time.time() - t0, S.CONNECT_TIMEOUT_S + 2)

    def test_health_reports_dead_machine_without_failing(self):
        S.set_config({"machines": {"dead": dict(DEAD, label="x")}, "routes": {}, "triage": {}})
        r = call("health", {})
        self.assertFalse(is_error(r))
        out = json.loads(text_of(r))
        self.assertFalse(out["hosts"][0]["reachable"])
        self.assertIn("unreachable", out["hosts"][0]["error"])
        self.assertFalse(out["code_tools"]["enabled"])

    def test_route_with_all_targets_dead(self):
        S.set_config({"machines": {"dead": DEAD}, "triage": {"default": "quick"},
                      "routes": {"quick": [{"machine": "dead", "model": TINY_MODEL},
                                           {"machine": "missing", "model": TINY_MODEL}]}})
        r = call("route", {"prompt": "hi", "tier": "quick"})
        self.assertTrue(is_error(r))
        self.assertIn("unreachable", text_of(r))
        self.assertIn("not configured", text_of(r))


# ── Config and host rules (offline) ──────────────────────────────────────────

class ConfigTests(unittest.TestCase):

    def setUp(self):
        self._env = os.environ.get("HIVEMIND_CONFIG")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("HIVEMIND_CONFIG", None)
        else:
            os.environ["HIVEMIND_CONFIG"] = self._env
        S.STATE["config_override"] = None
        use_test_config()

    def write(self, name, content):
        path = os.path.join(TMP, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
        return path

    def test_builtin_default_is_localhost(self):
        cfg = S.load_config(force=True, candidates=[(os.path.join(TMP, "missing.json"), True)])
        self.assertEqual(S.default_machine(cfg), "localhost")
        self.assertEqual(S.resolve_host(None, cfg), ("localhost", "127.0.0.1", 11434))
        self.assertIn("built-in", S.STATE["config_path"])
        self.assertEqual(S.tier_names(cfg), ["quick", "interactive", "quality"])

    def test_search_order(self):
        S.STATE["config_override"] = os.path.join(TMP, "flag.json")
        os.environ["HIVEMIND_CONFIG"] = os.path.join(TMP, "env.json")
        cands = S.config_candidates()
        self.assertEqual([p for p, _ in cands], [
            os.path.join(TMP, "flag.json"),
            os.path.join(TMP, "env.json"),
            os.path.join(ROOT, "hivemind-config.json"),
            os.path.join(os.path.expanduser("~"), ".hivemind", "hivemind-config.json"),
        ])
        self.assertEqual([explicit for _, explicit in cands], [True, True, False, False])

    def test_first_usable_file_wins(self):
        bad = self.write("bad.json", "{not json")
        no_machines = self.write("no-machines.json", {"routes": {}})
        good = self.write("good.json", {"machines": {"good": {"host": "10.1.2.3"}}, "routes": {}})
        cfg = S.load_config(force=True, candidates=[(os.path.join(TMP, "missing.json"), True),
                                                    (bad, True), (no_machines, False), (good, False)])
        self.assertEqual(list(cfg["machines"]), ["good"])
        self.assertEqual(S.STATE["config_path"], good)
        self.assertEqual(S.resolve_host(None, cfg), ("good", "10.1.2.3", 11434))   # port defaults

    def test_example_config_is_consistent(self):
        with open(EXAMPLE_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for name, m in cfg["machines"].items():
            S.machine_address(name, m)
            self.assertTrue(S._is_private_address(m["host"]), m["host"])
        self.assertIn(cfg["triage"]["machine"], cfg["machines"])
        self.assertEqual(list(cfg["routes"]), ["quick", "interactive", "quality"])
        for targets in cfg["routes"].values():
            for t in targets:
                self.assertIn(t["machine"], cfg["machines"])
                self.assertTrue(t["model"])
        S.set_config(cfg)
        route = [t for t in S.tool_definitions() if t["name"] == "route"][0]
        self.assertEqual(route["inputSchema"]["properties"]["tier"]["enum"],
                         ["auto", "quick", "interactive", "quality"])

    def test_configured_public_host_allowed_only_as_configured(self):
        S.set_config({"machines": {"gpu": {"host": "ollama.example.net", "port": 11434}}, "routes": {}})
        cfg = S.load_config()
        self.assertEqual(S.resolve_host("gpu", cfg), ("gpu", "ollama.example.net", 11434))
        self.assertEqual(S.resolve_host("ollama.example.net:11434", cfg)[0], "gpu")
        for other in ("ollama.example.net:9999", "other.example.net"):
            with self.assertRaises(S.ToolError):
                S.resolve_host(other, cfg)

    def test_ipv6_hosts(self):
        cfg = S.load_config()
        self.assertEqual(S.resolve_host("[fd00::5]:11435", cfg), ("fd00::5", "fd00::5", 11435))
        self.assertEqual(S.resolve_host("[::1]", cfg), ("::1", "::1", 11434))
        with self.assertRaises(S.ToolError):
            S.resolve_host("[2606:4700::1111]:11434", cfg)

    def test_machine_without_host_is_a_tool_error(self):
        S.set_config({"machines": {"broken": {"port": 11434}}, "routes": {}})
        r = call("list_models", {"host": "broken"})
        self.assertTrue(is_error(r))
        self.assertIn("no 'host'", text_of(r))
        out = json.loads(text_of(call("health", {})))
        self.assertFalse(out["hosts"][0]["reachable"])


# ── Code tools (offline; hmc's six tools behind --code-tools) ────────────────

class CodeToolsTests(unittest.TestCase):

    def setUp(self):
        self.proj = tempfile.mkdtemp(dir=TMP)
        self.real = os.path.realpath(self.proj)

    def tearDown(self):
        S.disable_code_tools()

    def tool_names(self):
        return [t["name"] for t in S.handle(rpc("tools/list"))["result"]["tools"]]

    def test_off_by_default(self):
        self.assertEqual(self.tool_names(), FIVE)
        r = call("read_file", {"path": "x"})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertEqual(S.code_tools_status(), {"enabled": False})

    def test_listed_with_annotations_and_approval_flag(self):
        st = S.enable_code_tools(self.proj)
        self.assertTrue(st["enabled"], st)
        self.assertEqual(self.tool_names(), FIVE + list(S.CODE_TOOL_NAMES))
        by = {t["name"]: t for t in S.handle(rpc("tools/list"))["result"]["tools"]}
        for n in ("write_file", "edit_file", "run_command"):
            self.assertIs(by[n]["_meta"]["anthropic/requiresUserInteraction"], True, n)
            self.assertTrue(by[n]["annotations"]["destructiveHint"], n)
            self.assertFalse(by[n]["annotations"]["readOnlyHint"], n)
        for n in ("read_file", "list_files", "search_files"):
            self.assertNotIn("_meta", by[n])
            self.assertTrue(by[n]["annotations"]["readOnlyHint"], n)
        self.assertIn(self.real, by["read_file"]["description"])
        init = S.handle(rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
        self.assertIn("Code tools are enabled", init["result"]["instructions"])

    def test_project_dir_defaults_to_claude_project_dir(self):
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self.proj
        try:
            self.assertEqual(S.enable_code_tools(None)["project_dir"], self.real)
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old

    def test_refuses_home_root_and_missing_dirs(self):
        for d in (os.path.expanduser("~"), os.path.abspath(os.sep), os.path.join(TMP, "nope")):
            st = S.enable_code_tools(d)
            self.assertFalse(st["enabled"], d)
            self.assertTrue(st["reason"], d)
            self.assertEqual(self.tool_names(), FIVE)

    def test_file_tools_stay_inside_the_project(self):
        S.enable_code_tools(self.proj)
        r = call("write_file", {"path": "sub/a.txt", "content": "alpha\nbeta\n"})
        self.assertFalse(is_error(r), text_of(r))
        self.assertIn("Successfully wrote", text_of(r))
        with open(os.path.join(self.proj, "sub", "a.txt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "alpha\nbeta\n")
        self.assertIn("beta", text_of(call("read_file", {"path": "sub/a.txt"})))
        r = call("edit_file", {"path": "sub/a.txt", "old_string": "beta", "new_string": "gamma"})
        self.assertFalse(is_error(r), text_of(r))
        self.assertIn("sub/a.txt", text_of(call("list_files", {"pattern": "**/*.txt"})))
        self.assertIn("sub/a.txt:2:gamma", text_of(call("search_files", {"pattern": "gam+a"})))
        for args in ({"path": "../outside.txt", "content": "x"},
                     {"path": os.path.join(TMP, "absolute.txt"), "content": "x"}):
            r = call("write_file", args)
            self.assertTrue(is_error(r), args)
            self.assertIn("outside the project", text_of(r))
        self.assertFalse(os.path.exists(os.path.join(TMP, "outside.txt")))
        self.assertTrue(is_error(call("read_file", {"path": "missing.txt"})))

    def test_run_command(self):
        S.enable_code_tools(self.proj)
        r = call("run_command", {"command": "echo hello"})
        self.assertFalse(is_error(r), text_of(r))
        self.assertIn("hello", text_of(r))
        r = call("run_command", {"command": "exit 3"})
        self.assertFalse(is_error(r))                      # the command ran; its exit code is reported
        self.assertIn("Exit code: 3", text_of(r))
        r = call("run_command", {"command": "rm -rf /"})
        self.assertTrue(is_error(r))
        self.assertIn("blocked", text_of(r))


# ── Live against one Ollama (skipped when it does not answer) ────────────────

@needs_live
class LiveTests(unittest.TestCase):

    def setUp(self):
        S.set_config(live_config(), "(live test config)")

    def tearDown(self):
        use_test_config()

    def need_tiny(self):
        if TINY_MODEL not in LIVE_MODELS:
            self.skipTest("{} not on {}:{} (set HIVEMIND_TEST_MODEL)".format(TINY_MODEL, LIVE["host"], LIVE["port"]))

    def test_health_shows_live_host(self):
        out = json.loads(text_of(call("health", {})))
        live = [h for h in out["hosts"] if h["machine"] == "live"][0]
        self.assertTrue(live["reachable"], live)
        self.assertGreater(live["model_count"], 0)
        self.assertIn("latency_ms", live)
        self.assertIn("ollama_version", live)
        self.assertIn("tiers", out)

    def test_list_models_default_host(self):
        out = json.loads(text_of(call("list_models", {})))
        self.assertEqual(out["machine"], "live")
        names = {m["name"] for m in out["models"]}
        self.assertEqual(names, LIVE_MODELS)
        self.assertTrue(all(isinstance(m["size_gb"], float) for m in out["models"]))

    def test_ask_local_tiny_model(self):
        self.need_tiny()
        r = call("ask_local", {"prompt": "What is 2+2? Answer with just the number.",
                               "model": TINY_MODEL, "host": "live", "max_tokens": 8,
                               "temperature": 0})
        self.assertFalse(is_error(r), text_of(r))
        out = json.loads(text_of(r))
        # Mechanics only: a 1B model's arithmetic is not under test (one has answered
        # "2 (as a numerical value)" here - the advisory-only rule in one line).
        self.assertTrue(out["text"].strip(), out)
        self.assertNotIn("warning", out)
        self.assertEqual(out["machine"], "live")
        self.assertEqual(out["model"], TINY_MODEL)
        self.assertIsInstance(out["eval_duration_ms"], int)
        self.assertIn("advisory", out)

    def test_ask_local_missing_model_is_error(self):
        r = call("ask_local", {"prompt": "hi", "model": "no-such-model:0b", "host": "live"})
        self.assertTrue(is_error(r))
        self.assertIn("not found", text_of(r))

    def test_embed_dimension(self):
        if not any(n.startswith("nomic-embed-text") for n in LIVE_MODELS):
            self.skipTest("nomic-embed-text not on {}:{}".format(LIVE["host"], LIVE["port"]))
        r = call("embed", {"text": "The quick brown fox.", "host": "live", "round": 4})
        self.assertFalse(is_error(r), text_of(r))
        out = json.loads(text_of(r))
        self.assertEqual(out["dimension"], 768)
        self.assertEqual(len(out["vector"]), 768)
        self.assertTrue(all(isinstance(v, float) for v in out["vector"]))
        self.assertLess(len(text_of(r)), 12000)
        r2 = call("embed", {"text": "x", "host": "live", "include_vector": False})
        self.assertNotIn("vector", json.loads(text_of(r2)))

    def test_route_falls_back_past_dead_host_and_missing_model(self):
        self.need_tiny()
        cfg = live_config({"quick": [{"machine": "dead", "model": TINY_MODEL},
                                     {"machine": "live", "model": "no-such-model:0b"},
                                     {"machine": "live", "model": TINY_MODEL}]})
        cfg["machines"]["dead"] = DEAD
        S.set_config(cfg)
        r = call("route", {"prompt": "Say the single word: pong", "tier": "quick",
                           "max_tokens": 8, "temperature": 0})
        self.assertFalse(is_error(r), text_of(r))
        out = json.loads(text_of(r))
        self.assertEqual(out["tier"], "quick")
        self.assertEqual(out["machine"], "live")
        self.assertEqual(out["model"], TINY_MODEL)
        results = [t["result"] for t in out["fallback_path"]]
        self.assertTrue(results[0].startswith("unreachable"), results)
        self.assertEqual(results[1], "model not installed on host")
        self.assertEqual(results[2], "answered")

    def test_route_auto_triage(self):
        self.need_tiny()
        r = call("route", {"prompt": "What is 1+1?", "max_tokens": 8, "temperature": 0})
        self.assertFalse(is_error(r), text_of(r))
        out = json.loads(text_of(r))
        self.assertIn(out["tier"], ("quick", "interactive", "quality"))
        self.assertTrue(out["tier_chosen_by"].startswith(("triage", "default")), out["tier_chosen_by"])


# ── The real stdio server in a subprocess ────────────────────────────────────

class StdioSubprocessTests(unittest.TestCase):

    def run_server(self, stdin_bytes, args=(), timeout=60):
        return subprocess.run([sys.executable, SERVER] + list(args), input=stdin_bytes,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, env=server_env())

    @staticmethod
    def frames(msgs):
        return "".join(json.dumps(m) + "\n" for m in msgs).encode("utf-8")

    def test_newline_delimited_handshake_and_clean_stdout(self):
        msgs = [
            initialize_msg(1),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "list_models", "arguments": {"host": "127.0.0.1:9"}}},
        ]
        p = self.run_server(self.frames(msgs))
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        lines = [l for l in p.stdout.decode("utf-8").split("\n") if l.strip()]
        self.assertEqual(len(lines), 5, p.stdout)       # the notification gets no reply
        for l in lines:
            json.loads(l)                                 # stdout is JSON-RPC only
        by_id = {json.loads(l)["id"]: json.loads(l) for l in lines}
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(by_id[2]["result"], {})
        self.assertEqual([t["name"] for t in by_id[3]["result"]["tools"]], FIVE)
        self.assertEqual(by_id[4]["error"]["code"], -32601)
        self.assertTrue(by_id[5]["result"]["isError"])

    def test_content_length_framing_is_tolerated(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8")
        data = b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        p = self.run_server(data)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(p.stdout.startswith(b"Content-Length: "), p.stdout)
        payload = p.stdout.split(b"\r\n\r\n", 1)[1].strip()
        self.assertEqual(json.loads(payload)["result"], {})

    def test_garbage_line_gets_parse_error_and_server_survives(self):
        data = b"this is not json\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}).encode() + b"\n"
        p = self.run_server(data)
        lines = [json.loads(l) for l in p.stdout.decode().split("\n") if l.strip()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["result"], {})

    def test_self_test_writes_nothing_to_stdout(self):
        p = self.run_server(b"", ["--self-test"])
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")
        self.assertIn(b"tools: health, list_models, ask_local, route, embed", p.stderr)
        self.assertIn(TEST_CONFIG_FILE.encode("utf-8"), p.stderr)

    def test_config_flag_beats_env(self):
        other = os.path.join(TMP, "flag-config.json")
        with open(other, "w", encoding="utf-8") as f:
            json.dump({"machines": {"flagged": {"host": "10.9.9.9"}}, "routes": {}}, f)
        p = self.run_server(b"", ["--config", other, "--self-test"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(b"config: " + other.encode("utf-8"), p.stderr)
        self.assertIn(b"default host: flagged", p.stderr)

    def test_project_dir_needs_code_tools(self):
        p = self.run_server(b"", ["--project-dir", TMP])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, b"")

    def test_code_tools_keep_stdout_clean(self):
        # In trust mode hmc prints "[auto-approved] ..." - on stdout that would
        # corrupt the protocol stream (the earlier wrapper did exactly that).
        proj = tempfile.mkdtemp(dir=TMP)
        msgs = [
            initialize_msg(1),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "write_file", "arguments": {"path": "a.txt", "content": "hello\n"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "run_command", "arguments": {"command": "echo hi"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        ]
        p = self.run_server(self.frames(msgs), ["--code-tools", "--project-dir", proj])
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = [l for l in p.stdout.decode("utf-8").split("\n") if l.strip()]
        by_id = {json.loads(l)["id"]: json.loads(l) for l in lines}   # every line is JSON
        self.assertEqual(sorted(by_id), [1, 2, 3, 4])
        self.assertIn("Successfully wrote", by_id[2]["result"]["content"][0]["text"])
        self.assertIn("hi", by_id[3]["result"]["content"][0]["text"])
        self.assertEqual(len(by_id[4]["result"]["tools"]), 11)
        with open(os.path.join(proj, "a.txt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello\n")

    def test_run_command_cannot_read_the_protocol_stream(self):
        # The command sleeps, then reads stdin. Meanwhile the client sends a ping.
        # The ping must reach the server, not the command.
        proj = tempfile.mkdtemp(dir=TMP)
        cmd = '"{}" -c "import sys, time; time.sleep(1.5); print(repr(sys.stdin.read()))"'.format(sys.executable)
        p = subprocess.Popen([sys.executable, SERVER, "--code-tools", "--project-dir", proj],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=server_env())
        killer = threading.Timer(60, p.kill)
        killer.start()
        try:
            p.stdin.write(self.frames([initialize_msg(1)]))
            p.stdin.flush()
            self.assertEqual(json.loads(p.stdout.readline())["id"], 1)   # server is up
            p.stdin.write(self.frames([{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                        "params": {"name": "run_command", "arguments": {"command": cmd}}}]))
            p.stdin.flush()
            time.sleep(0.5)
            p.stdin.write(self.frames([{"jsonrpc": "2.0", "id": 3, "method": "ping"}]))
            p.stdin.flush()
            p.stdin.close()
            out = p.stdout.read()
            p.wait(timeout=10)
        finally:
            killer.cancel()
            if p.poll() is None:
                p.kill()
            p.stdout.close()
        by_id = {json.loads(l)["id"]: json.loads(l) for l in out.decode("utf-8").split("\n") if l.strip()}
        self.assertEqual(by_id[2]["result"]["content"][0]["text"].strip(), "''")
        self.assertEqual(by_id.get(3, {}).get("result"), {}, by_id)

    @unittest.skipIf(sys.platform == "win32", "POSIX signals")
    def test_sigint_exits_cleanly(self):
        # Claude Code stops stdio servers with SIGINT, then SIGTERM, then SIGKILL.
        p = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=server_env())
        try:
            p.stdin.write(self.frames([initialize_msg(1)]))
            p.stdin.flush()
            self.assertEqual(json.loads(p.stdout.readline())["id"], 1)
            p.send_signal(signal.SIGINT)
            rc = p.wait(timeout=10)
            err = p.stderr.read()
        finally:
            if p.poll() is None:
                p.kill()
            for s in (p.stdin, p.stdout, p.stderr):
                try:
                    s.close()
                except Exception:
                    pass
        self.assertEqual(rc, 0)
        self.assertNotIn(b"Traceback", err)


if __name__ == "__main__":
    print("server: {}".format(SERVER))
    print("live host: {}:{} ({})".format(
        LIVE["host"], LIVE["port"],
        "{} models".format(len(LIVE_MODELS)) if LIVE_MODELS is not None else "unreachable - live tests skipped"))
    print("tiny model: {}{}".format(TINY_MODEL, "" if LIVE_MODELS is None or TINY_MODEL in LIVE_MODELS
                                    else " (not on the live host - its tests skip)"))
    unittest.main(verbosity=2)

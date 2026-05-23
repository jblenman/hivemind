#!/usr/bin/env python
"""
hivemind.py - Local AI Triage Router

Routes prompts to the best available model across your local machine fleet.
A fast model classifies prompt complexity, then routes to the optimal
model/machine combination. Streams responses in real time.

Usage:
    python hivemind.py "explain python decorators"
    python hivemind.py -r quality "design a REST API for..."
    python hivemind.py -m gpt-oss:20b "hard question"
    python hivemind.py -i                          # interactive mode
    python hivemind.py --status                    # check machines
    echo "prompt" | python hivemind.py

Config: hivemind-config.json (same directory as this script)
Requires: Ollama running on configured machines. Python 3.8+, no deps.
"""

import sys
import json
import os
import argparse
import urllib.request
import urllib.error
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "hivemind-config.json")

# Fix Windows CP1252 encoding — models output unicode that CP1252 can't handle
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

TRIAGE_SYSTEM = (
    "You are a prompt classifier. Respond with ONLY one word.\n"
    "Classify the user's prompt into one of these categories:\n"
    "- quick: simple syntax, one-liners, short factual lookups, trivial fixes\n"
    "- interactive: medium tasks, explanations, moderate code, debugging help\n"
    "- quality: hard problems, architecture design, complex debugging, large code\n"
    "Respond with ONLY: quick, interactive, or quality"
)


def load_config(path):
    with open(path, "r") as f:
        return json.load(f)


def check_machine(host, port, timeout=3):
    """Check if an Ollama instance is reachable. Returns model list or None."""
    url = "http://{}:{}/api/tags".format(host, port)
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url), timeout=timeout)
        data = json.loads(resp.read().decode("utf-8"))
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return None


def classify(prompt, config):
    """Classify prompt complexity using the triage model. Returns route name."""
    triage = config.get("triage", {})
    machine = config["machines"].get(triage.get("machine", ""), {})
    if not machine:
        return triage.get("default", "interactive")

    host, port = machine["host"], machine["port"]
    model = triage.get("model", "granite3-moe:1b")

    data = {
        "model": model,
        "system": TRIAGE_SYSTEM,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 10, "temperature": 0.1}
    }
    payload = json.dumps(data).encode("utf-8")
    url = "http://{}:{}/api/generate".format(host, port)
    req = urllib.request.Request(url, data=payload,
                                headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read().decode("utf-8"))
        word = result.get("response", "").strip().lower().split()[0]
        # Strip punctuation
        word = word.strip(".,!?;:'\"")
        if word in config.get("routes", {}):
            return word
    except Exception:
        pass

    return triage.get("default", "interactive")


def find_target(route_name, config):
    """Find best available machine+model for a route. Returns (name, host, port, model) or Nones."""
    routes = config.get("routes", {})
    targets = routes.get(route_name, routes.get("interactive", []))

    for t in targets:
        machine = config["machines"].get(t["machine"], {})
        if not machine:
            continue
        host, port = machine["host"], machine["port"]
        available = check_machine(host, port)
        if available is not None:
            return t["machine"], host, port, t["model"]

    return None, None, None, None


def find_model_anywhere(model, config):
    """Find a specific model on any reachable machine."""
    for name, machine in config["machines"].items():
        available = check_machine(machine["host"], machine["port"])
        if available is not None:
            return name, machine["host"], machine["port"], model
    return None, None, None, None


def stream_generate(host, port, model, prompt, system=None):
    """Stream a response from Ollama. Returns full response text."""
    data = {"model": model, "prompt": prompt, "stream": True}
    if system:
        data["system"] = system

    url = "http://{}:{}/api/generate".format(host, port)
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                headers={"Content-Type": "application/json"})

    try:
        resp = urllib.request.urlopen(req, timeout=600)
        full = ""
        start = time.time()
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line.decode("utf-8"))
                token = chunk.get("response", "")
                if token:
                    sys.stdout.write(token)
                    sys.stdout.flush()
                    full += token
                if chunk.get("done", False):
                    elapsed = time.time() - start
                    eval_count = chunk.get("eval_count", 0)
                    tps = ""
                    if eval_count and elapsed > 0:
                        tps = ", {:.1f} tok/s".format(eval_count / elapsed)
                    sys.stderr.write(
                        "\n[{:.1f}s{}]\n".format(elapsed, tps)
                    )
                    break
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return full
    except urllib.error.URLError as e:
        sys.stderr.write("\nConnection error: {}\n".format(e))
        return None
    except Exception as e:
        sys.stderr.write("\nError: {}\n".format(e))
        return None


def show_status(config):
    """Show status of all configured machines."""
    for name, machine in config["machines"].items():
        label = machine.get("label", "")
        models = check_machine(machine["host"], machine["port"])
        if models:
            short = [m.split(":")[0] for m in sorted(models)]
            # deduplicate
            seen = set()
            unique = []
            for m in short:
                if m not in seen:
                    seen.add(m)
                    unique.append(m)
            print("  {} ({}) -- ONLINE, {} models: {}".format(
                name, label or "{}:{}".format(machine["host"], machine["port"]),
                len(models), ", ".join(unique)
            ))
        else:
            print("  {} ({}) -- OFFLINE".format(
                name, label or "{}:{}".format(machine["host"], machine["port"])
            ))


def process_one(prompt, config, route_override=None, model_override=None, system=None):
    """Process a single prompt. Returns True if successful."""
    if model_override:
        mname, host, port, model = find_model_anywhere(model_override, config)
        route_name = "direct"
    elif route_override:
        route_name = route_override
        mname, host, port, model = find_target(route_name, config)
    else:
        sys.stderr.write("Classifying... ")
        route_name = classify(prompt, config)
        sys.stderr.write("[{}]\n".format(route_name))
        mname, host, port, model = find_target(route_name, config)

    if not host:
        sys.stderr.write("No available machine for route '{}'.\n".format(route_name))
        return False

    sys.stderr.write("{} on {} ({}:{})\n\n".format(model, mname, host, port))
    result = stream_generate(host, port, model, prompt, system=system)
    return result is not None


def interactive_mode(config, system=None):
    """Interactive chat loop."""
    print("Hivemind Interactive Mode (Ctrl+C to exit)")
    print("Commands: /r <route> <prompt>  /m <model> <prompt>  /status  /quit")
    print("Routes: quick, interactive, quality")
    print()

    while True:
        try:
            prompt = input("You: ").strip()
            if not prompt:
                continue
            if prompt.lower() in ("/quit", "/exit", "/q"):
                break
            if prompt == "/status":
                show_status(config)
                continue

            route = None
            model = None
            if prompt.startswith("/r "):
                parts = prompt.split(None, 2)
                if len(parts) >= 3:
                    route, prompt = parts[1], parts[2]
            elif prompt.startswith("/m "):
                parts = prompt.split(None, 2)
                if len(parts) >= 3:
                    model, prompt = parts[1], parts[2]

            print()
            process_one(prompt, config, route_override=route,
                        model_override=model, system=system)
            print()

        except KeyboardInterrupt:
            print("\nBye!")
            break
        except EOFError:
            break


def main():
    parser = argparse.ArgumentParser(
        description="Local AI Triage Router",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  %(prog)s \"explain python decorators\"\n"
               "  %(prog)s -r quality \"design a microservice\"\n"
               "  %(prog)s -m gpt-oss:20b \"hard question\"\n"
               "  %(prog)s -i\n"
               "  %(prog)s --status"
    )
    parser.add_argument("prompt", nargs="*", help="The prompt to send")
    parser.add_argument("-r", "--route",
                        help="Force a route (skip triage): quick, interactive, quality")
    parser.add_argument("-m", "--model", help="Force a specific model (skip routing)")
    parser.add_argument("-s", "--system", help="System prompt")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="Interactive chat mode")
    parser.add_argument("--status", action="store_true",
                        help="Show machine and model status")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="Path to config file")

    args = parser.parse_args()
    config = load_config(args.config)

    if args.status:
        show_status(config)
        return

    if args.interactive:
        interactive_mode(config, system=args.system)
        return

    # Get prompt from args or stdin
    if args.prompt:
        prompt = " ".join(args.prompt)
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    else:
        parser.print_help()
        return

    if not prompt:
        sys.stderr.write("Empty prompt.\n")
        return

    process_one(prompt, config, route_override=args.route,
                model_override=args.model, system=args.system)


if __name__ == "__main__":
    main()

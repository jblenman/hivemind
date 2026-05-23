# MCP Wrapper Experiment Notes

I wrote [`docs/MCP_WRAPPER_SPEC.md`](MCP_WRAPPER_SPEC.md) as a self-contained, concrete specification — imports listed, skeleton in place, acceptance criteria spelled out — with the intent of having hivemind's own coding assistant (`hmc`) implement the wrapper from the spec. "Hivemind writing hivemind" as a small demonstration that the local fleet can do meaningful agentic code work end-to-end.

## What actually happened

Invocation:

```bash
hmc --one-shot --trust -r quality \
  "Read docs/MCP_WRAPPER_SPEC.md carefully end-to-end, then read \
   hivemind-code.py to find the TOOLS list and execute_tool function. \
   Then create hivemind_mcp.py at the repo root by writing the file."
```

Routed to `gpt-oss:20b` on localhost. Ran for ~10 minutes.

The agent did the right things initially:

- Read the spec.
- Searched `hivemind-code.py` for `TOOLS`, `execute_tool`, and individual tool function names.
- Read multiple offset-bounded slices of `hivemind-code.py` to understand the dispatch table and the `TOOLS` schema.

The agent did not write the wrapper. After 11 tool calls (5 reads, 6 searches) the loop exited without ever invoking `write_file`. Last action was another `read_file` on a section of `hivemind-code.py` it had already viewed.

## What I think happened

This was a clean "analysis paralysis" failure mode for a smaller local model on an agentic task that requires committing to a long write after extended reading. The model kept gathering context past the point of sufficiency — every additional read was a hedge against uncertainty rather than progress toward output. Speculations on contributing factors:

- **Tool-call cost asymmetry.** Reads are cheap and feel safe; writes feel committal. A model not strongly anchored on "produce output now" will skew toward more reads.
- **Spec verbosity vs. action density.** The spec is thorough — imports, skeleton, acceptance criteria — and re-reading it competes with writing the file. A leaner spec ("write the file matching this skeleton") might push faster commitment.
- **Context pressure.** After 22 messages of full file reads, the model's context was likely heavy with already-known code; the cost of re-orienting before writing may have exceeded the patience budget the loop allowed.

## What I did instead

I wrote `hivemind_mcp.py` by hand following the same spec. It's ~120 lines, syntactically clean, and registers six tools with the official `mcp` Python SDK. It compiles cleanly and would need `pip install mcp` + a `claude_desktop_config.json` entry to run.

The experiment is more useful as a calibration data point than as a feature: the 20B local model is good at *understanding* a moderately complex task end-to-end, less good at *finishing* it without scaffolding that forces commitment. For agentic work where I need autonomous completion, a frontier model is still the right tool. For tighter loops where I drive and the local model assists with concrete sub-tasks, the local fleet is genuinely useful.

## Future directions (if I revisit)

- **Tighter prompt:** point at the skeleton in the spec and say "fill in the six handler bodies." Less reading required, more incremental commitment.
- **Chunked task:** "first, write a stub with imports and Server() instantiation. Then read it back and add the list_tools handler. Then the call_tool handler. Then main()." Each step is small enough to commit on.
- **Different model:** `qwen3-coder:30b` (MoE, faster, code-tuned) might be more decisive on writes.
- **Larger model:** test if the same prompt succeeds on a frontier model in cloud — if yes, the failure mode is model-size-specific, not prompt-specific.

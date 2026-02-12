# Running Kaizen Learning Loop Experiments

This document explains how to run SRE investigation scenarios with the Kaizen agentic memory system to measure guideline-driven improvement.

## Overview

The **learning loop** works in two trials:

1. **Trial 1 (No Guidelines)** — The agent investigates with no prior knowledge. Kaizen's `get_guidelines` returns empty. After the run, the agent's trajectory is saved and Kaizen generates investigation tips via LLM critique.
2. **Trial 2 (With Guidelines)** — The agent investigates the same scenario. This time, `get_guidelines` returns tips learned from Trial 1 (e.g., "always examine ConfigMaps for feature flag changes"). The hypothesis is that T2 produces better root-cause analysis.

## Prerequisites

### Environment Variables

Create a `.env` file at the project root (already in `.gitignore`):

```bash
# LLM provider for Codex agent (the SRE investigator)
OPENAI_API_KEY=<your-key>

# LLM provider for Kaizen tip generation (via LiteLLM proxy or direct)
LITELLM_BASE_URL=<base-url>
LITELLM_API_KEY=<api-key>

# Kaizen model config
KAIZEN_TIPS_MODEL=<model-id>              # e.g., openai/gpt-4.1
KAIZEN_CONFLICT_RESOLUTION_MODEL=<model-id>
KAIZEN_CUSTOM_LLM_PROVIDER=openai         # or openrouter, etc.
```

### Dependencies

Kaizen must be installed as an editable dependency:

```bash
uv sync   # installs Kaizen from ./Kaizen via [tool.uv.sources] path dep
```

> **Note:** `sentence-transformers` (Kaizen dependency) requires a Rust compiler for `tokenizers`. Install via `rustup` if builds fail.

### ITBench Snapshots

Scenario snapshots must exist under `ITBench-Lite/snapshots/sre/`. Each scenario directory contains alert files, metrics, traces, and `k8s_objects_raw.tsv`.

## Fresh Experiment Setup

To start a clean experiment, wipe the Kaizen vector database and output directories:

```bash
# Remove stored guidelines and trajectories
rm -f kaizen_data/kaizen.milvus.db

# Remove previous agent outputs (optional)
rm -rf /tmp/outputs/
```

## Running Trial 1 (No Guidelines)

Run a scenario with `--collect-traces` so the OTEL trace data is captured for Kaizen:

```bash
# Scenario 2, Trial 1
uv run zero \
  --workspace /tmp/outputs/2/1 \
  --read-only-dir ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-2 \
  --collect-traces

# Scenario 5, Trial 1
uv run zero \
  --workspace /tmp/outputs/5/1 \
  --read-only-dir ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-5 \
  --collect-traces
```

**What happens:**
- The agent calls `get_guidelines` in Phase 0 — returns empty (no prior knowledge)
- The agent investigates using available MCP tools
- Results are written to `<workspace>/agent_output.json`
- In the `finally` block, `kaizen_integration.py` parses OTEL traces into OpenAI message format, saves the trajectory to Kaizen, and triggers tip generation
- Tips are stored in the Milvus vector DB under namespace `sre_scenario_N`

## What Happens After Trial 1

After a successful T1 run with `--collect-traces`, Kaizen's critical reviewer LLM:

1. **Analyzes the trajectory** — Examines the full investigation conversation
2. **Generates tips** — Identifies blind spots and missed opportunities (e.g., "the agent never examined ConfigMaps")
3. **Stores guidelines** — Tips are embedded and stored in Milvus, keyed to the scenario namespace

You can verify guidelines were saved by checking the `kaizen_data/` directory exists and `kaizen.milvus.db` has grown in size.

## Running Trial 2 (With Guidelines)

Run the same scenario again with a different workspace path (to avoid overwriting T1 output):

```bash
# Scenario 2, Trial 2
uv run zero \
  --workspace /tmp/outputs/2/2 \
  --read-only-dir ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-2 \
  --collect-traces

# Scenario 5, Trial 2
uv run zero \
  --workspace /tmp/outputs/5/2 \
  --read-only-dir ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-5 \
  --collect-traces
```

**What happens differently:**
- Phase 0 `get_guidelines` now returns tips from Trial 1 (semantic search matches the incident description)
- The agent uses these guidelines to inform its investigation strategy
- For example, a guideline like "Always examine ConfigMaps and feature flags" causes the agent to check `k8s_objects_raw.tsv` — something it may have skipped in T1

## Running Evaluations

Use the ITBench-Evaluations tool (git submodule) to compare agent output against ground truth:

```bash
cd ITBench-Evaluations

# Evaluate Trial 1
uv run python -m itbench_evaluations \
  --ground-truth ../ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-2/ground_truth.yaml \
  --outputs /tmp/outputs/2/1/agent_output.json

# Evaluate Trial 2
uv run python -m itbench_evaluations \
  --ground-truth ../ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-2/ground_truth.yaml \
  --outputs /tmp/outputs/2/2/agent_output.json
```

Compare the scores to measure the impact of Kaizen guidelines on investigation quality.

## How Namespaces Work

Kaizen namespaces isolate guidelines per scenario. The namespace is derived from the workspace path:

| Workspace Path | Namespace |
|---|---|
| `/tmp/outputs/2/1` | `sre_scenario_2` |
| `/tmp/outputs/Scenario-5/trial-1` | `sre_scenario_5` |
| `/tmp/outputs/work` | `sre_default` |

Logic is in `zero/config.py::_derive_kaizen_namespace()`. The first numeric or `Scenario-N` component in the path determines the scenario ID. Trial 1 and Trial 2 of the same scenario share the same namespace — this is intentional so T2 can retrieve T1's guidelines.

## Disabling Kaizen

Two ways to disable Kaizen without code changes:

1. **Remove from prompt frontmatter** — Edit `zero/zero-config/prompts/react_shell_investigation.md` and remove `kaizen` from the `mcp_servers` list
2. **Comment out MCP server** — Comment the `[mcp_servers.kaizen]` section in `zero/zero-config/config.toml`

Omitting `--collect-traces` prevents trajectory saving but the agent will still call `get_guidelines` if Kaizen is configured.

## Gotchas

- **Milvus Lite file lock**: Only one process can access `kaizen.milvus.db` at a time. Run scenarios sequentially, not in parallel.
- **Namespace isolation**: Guidelines from Scenario-2 won't appear in Scenario-5 queries (different namespaces). Cross-scenario transfer only happens if the Milvus semantic search finds relevant matches.
- **Trace collection is required**: Without `--collect-traces`, the post-run trajectory saving has no OTEL data to parse. It will fall back to `AGENTS.md` + `agent_output.json`, which produces lower-quality tips.
- **Stale guidelines**: If you want a completely fresh T1, delete `kaizen_data/kaizen.milvus.db` first. Otherwise, guidelines from previous experiments persist.
- **LLM provider for tips**: Kaizen tip generation uses its own LLM call (configured via `KAIZEN_TIPS_MODEL`). This is separate from the Codex agent's LLM. Make sure the `LITELLM_*` env vars are set.

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
KAIZEN_TIPS_MODEL=<model-id>              # e.g., Azure/gpt-4.1
KAIZEN_CONFLICT_RESOLUTION_MODEL=<model-id>
KAIZEN_CUSTOM_LLM_PROVIDER=openai         # or openrouter, etc.
```

### Dependencies

Kaizen must be installed as an editable dependency:

```bash
uv sync   # installs Kaizen from ./Kaizen via [tool.uv.sources] path dep
```

> **Note:** `sentence-transformers` (Kaizen dependency) requires a Rust compiler for `tokenizers`. Install via `rustup` if builds fail.

### Config.toml

The MCP server configuration in `zero/zero-config/config.toml` may need adjusting for your environment:

- **`python` → `python3`**: On macOS, the `python` binary may not exist. Change `command = "python"` to `command = "python3"` for both the `offline_incident_analysis` and `kaizen` MCP server entries.
- **`PYTHONPATH`**: Set `PYTHONPATH` to the full absolute path of the project root (e.g., `/Users/you/path/to/sre-kaizen`) in `[mcp_servers.offline_incident_analysis.env]`.

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

Run a scenario with `--collect-traces` and `--verbose` so trace data is captured and Kaizen status is visible:

```bash
# Source environment variables
source .env

# Scenario 2, Trial 1
uv run zero \
  --workspace /tmp/outputs/2/1 \
  --read-only-dir ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-2 \
  --collect-traces --verbose \
  --prompt-file zero/zero-config/prompts/react_shell_investigation.md \
  --variable "SNAPSHOT_DIRS=$(pwd)/ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-2" \
  -- exec -m "Azure/gpt-4.1"

# Scenario 5, Trial 1
uv run zero \
  --workspace /tmp/outputs/5/1 \
  --read-only-dir ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-5 \
  --collect-traces --verbose \
  --prompt-file zero/zero-config/prompts/react_shell_investigation.md \
  --variable "SNAPSHOT_DIRS=$(pwd)/ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-5" \
  -- exec -m "Azure/gpt-4.1"
```

**What happens:**
- The agent calls `get_guidelines` in Phase 0 — returns empty (no prior knowledge)
- The agent investigates using available MCP tools
- Results are written to `<workspace>/agent_output.json`
- In the `finally` block, `kaizen_integration.py` parses the Codex `--json` exec output (`stdout.log`) into OpenAI message format, saves the trajectory to Kaizen, and triggers tip generation
- Tips are stored in the Milvus vector DB under namespace `sre_scenario_N`
- With `--verbose`, you'll see Kaizen status output: message count, trajectory storage, and tip generation results (or errors with retry attempts)

## What Happens After Trial 1

After a successful T1 run, Kaizen's critical reviewer LLM:

1. **Analyzes the trajectory** — Examines the full investigation conversation (typically 50-100 messages including reasoning, tool calls, and results)
2. **Generates tips** — Identifies blind spots and missed opportunities (e.g., "the agent never examined ConfigMaps"). Retries up to 3 times on transient failures.
3. **Stores guidelines** — Tips are embedded and stored in Milvus, keyed to the scenario namespace

With `--verbose` you'll see output like:
```
Kaizen: Saving trajectory with 64 messages to namespace 'sre_scenario_5'
Kaizen: Stored 64 trajectory entities
Kaizen: Generated and stored 5 guidelines
```

## Running Trial 2 (With Guidelines)

Run the same scenario again with a different workspace path (to avoid overwriting T1 output):

```bash
# Source environment variables
source .env

# Scenario 2, Trial 2
uv run zero \
  --workspace /tmp/outputs/2/2 \
  --read-only-dir ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-2 \
  --collect-traces --verbose \
  --prompt-file zero/zero-config/prompts/react_shell_investigation.md \
  --variable "SNAPSHOT_DIRS=$(pwd)/ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-2" \
  -- exec -m "Azure/gpt-4.1"

# Scenario 5, Trial 2
uv run zero \
  --workspace /tmp/outputs/5/2 \
  --read-only-dir ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-5 \
  --collect-traces --verbose \
  --prompt-file zero/zero-config/prompts/react_shell_investigation.md \
  --variable "SNAPSHOT_DIRS=$(pwd)/ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-5" \
  -- exec -m "Azure/gpt-4.1"
```

**What happens differently:**
- Phase 0 `get_guidelines` now returns tips from Trial 1 (semantic search matches the incident description)
- The agent uses these guidelines to inform its investigation strategy
- For example, a guideline like "Actively seek out and document alternative hypotheses" causes the agent to investigate deeper — potentially finding ConfigMap changes it would have missed in T1

## Trajectory Parsing

Kaizen builds the agent trajectory from two possible sources, in priority order:

1. **`stdout.log`** (primary) — The Codex `--json` exec output containing the full conversation: agent reasoning (`agent_message`), MCP tool calls (`mcp_tool_call`), and shell commands (`command_execution`) with their results. Typically produces 50-100 messages.
2. **`traces.jsonl`** (legacy fallback) — OTEL protobuf log records. As of Codex v0.98, these contain only metadata events (`body: null`) and large protobuf payloads that fail to decode due to version mismatch. Not useful for trajectory extraction.
3. **`AGENTS.md` + `agent_output.json`** (final fallback) — If neither source produces messages, the prompt and final output are used. Produces only 2 messages.

The `--collect-traces` flag is still recommended as it captures OTEL metadata for potential future use, and ensures `stdout.log` is written to the `traces/` directory.

## Running Evaluations

Use the ITBench-Evaluations tool (git submodule) to compare agent output against ground truth:

```bash
cd ITBench-Evaluations

# Evaluate Trial 1
uv run python3 -m itbench_evaluations \
  --ground-truth ../ITBench-Lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/Scenario-2/ground_truth.yaml \
  --outputs /tmp/outputs/2/1/agent_output.json

# Evaluate Trial 2
uv run python3 -m itbench_evaluations \
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
- **`--verbose` recommended**: Without `--verbose`, Kaizen tip generation status and errors are silent. Always use `--verbose` to see how many messages were parsed, how many tips were generated, and any retry/failure information.
- **Stale guidelines**: If you want a completely fresh T1, delete `kaizen_data/kaizen.milvus.db` first. Otherwise, guidelines from previous experiments persist.
- **LLM provider for tips**: Kaizen tip generation uses its own LLM call (configured via `KAIZEN_TIPS_MODEL`). This is separate from the Codex agent's LLM. Make sure the `LITELLM_*` env vars are set.
- **`source .env` required**: Environment variables must be loaded before running. The `uv run zero` command does not auto-source `.env`.
- **Tip generation retries**: If the LLM returns an empty or malformed response, tip generation retries up to 3 times with exponential back-off (2s, 4s). Check `--verbose` output to confirm tips were stored.

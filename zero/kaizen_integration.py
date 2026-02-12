"""
Kaizen agentic memory integration for Zero.

Handles post-run trajectory saving: parses OTEL traces from Codex execution,
converts them to OpenAI conversation format, and saves to Kaizen for
tip generation and guideline learning.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from .config import _derive_kaizen_namespace


def parse_traces_to_messages(
    traces_file: Path,
    agents_md_file: Path,
    agent_output_file: Path | None = None,
) -> list[dict]:
    """Parse OTEL trace logs into OpenAI-format conversation messages.

    Reads the traces.jsonl file produced by Zero's OTEL collector and converts
    it into the message format expected by Kaizen's generate_tips() function:
      - {"role": "user", "content": "..."} for the task prompt
      - {"role": "assistant", "content": "..."} for reasoning steps
      - {"role": "assistant", "content": [{"type": "function_call", ...}]} for tool calls

    Falls back to a simplified trajectory (AGENTS.md + agent_output.json) if
    traces are empty or unparseable.

    Args:
        traces_file: Path to traces/traces.jsonl
        agents_md_file: Path to AGENTS.md (the substituted prompt)
        agent_output_file: Optional path to agent_output.json

    Returns:
        List of message dicts in OpenAI conversation format
    """
    # Read the original prompt as the user message
    user_prompt = ""
    if agents_md_file.exists():
        user_prompt = agents_md_file.read_text()

    if not user_prompt:
        user_prompt = "SRE incident investigation task"

    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    # Try to parse OTEL traces
    trace_messages = _extract_messages_from_traces(traces_file)

    if trace_messages:
        messages.extend(trace_messages)
    else:
        # Fallback: if traces are empty/unparseable, use agent_output as final response
        if agent_output_file and agent_output_file.exists():
            try:
                output_content = agent_output_file.read_text()
                messages.append({
                    "role": "assistant",
                    "content": f"Investigation complete. Final diagnosis:\n{output_content}",
                })
            except Exception:
                messages.append({
                    "role": "assistant",
                    "content": "Investigation completed (output could not be read).",
                })
        else:
            messages.append({
                "role": "assistant",
                "content": "Investigation completed (no output file found).",
            })

    return messages


def _extract_messages_from_traces(traces_file: Path) -> list[dict]:
    """Extract conversation messages from OTEL trace JSONL file.

    The traces.jsonl contains protobuf-decoded OTEL log records from Codex.
    Each line is a JSON object with nested resource_logs → scope_logs → log_records.

    Log record bodies and attributes contain the agent's reasoning and tool calls.
    """
    if not traces_file.exists():
        return []

    messages = []
    func_call_counter = 0

    try:
        with open(traces_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Navigate the OTEL structure
                data = record.get("data", {})
                resource_logs = data.get("resource_logs", [])

                for resource_log in resource_logs:
                    for scope_log in resource_log.get("scope_logs", []):
                        for log_record in scope_log.get("log_records", []):
                            msg = _log_record_to_message(log_record, func_call_counter)
                            if msg:
                                messages.append(msg)
                                if msg["role"] == "assistant" and isinstance(msg.get("content"), list):
                                    func_call_counter += 1
    except Exception:
        # If anything goes wrong parsing, return what we have
        pass

    return messages


def _log_record_to_message(log_record: dict, call_counter: int) -> dict | None:
    """Convert a single OTEL log record to an OpenAI message format.

    Codex OTEL logs contain agent reasoning in the body and structured data
    in attributes. We extract:
    - Text reasoning → assistant message with string content
    - Tool/function calls → assistant message with function_call content list
    """
    body = log_record.get("body", "")
    attributes = log_record.get("attributes", {})

    if not body and not attributes:
        return None

    # Check for tool/function call indicators in attributes
    event_type = attributes.get("codex.event_type", "")
    tool_name = attributes.get("codex.tool_name", "") or attributes.get("tool.name", "")
    tool_args = attributes.get("codex.tool_arguments", "") or attributes.get("tool.arguments", "")

    # If this looks like a function call
    if tool_name:
        call_id = attributes.get("codex.tool_call_id", f"call_{call_counter}")
        args_str = tool_args if isinstance(tool_args, str) else json.dumps(tool_args)
        return {
            "role": "assistant",
            "content": [
                {
                    "type": "function_call",
                    "id": call_id,
                    "function": {
                        "name": tool_name,
                        "arguments": args_str,
                    },
                }
            ],
        }

    # If this is a text body (reasoning), extract it as assistant message
    if isinstance(body, str) and body.strip():
        # Skip very short or metadata-only bodies
        if len(body.strip()) < 5:
            return None
        # Truncate very long bodies to avoid overwhelming tip generation
        content = body[:4000] + "..." if len(body) > 4000 else body
        return {"role": "assistant", "content": content}

    # If body is structured (dict/list), convert to string
    if isinstance(body, (dict, list)):
        content = json.dumps(body)
        if len(content) < 5:
            return None
        content = content[:4000] + "..." if len(content) > 4000 else content
        return {"role": "assistant", "content": content}

    return None


def save_trajectory_to_kaizen(
    *,
    traces_file: Path,
    agents_md_file: Path,
    agent_output_file: Path,
    namespace_id: str | None = None,
    workspace_dir: Path | None = None,
    verbose: bool = False,
) -> None:
    """Save the agent's trajectory to Kaizen for tip generation and learning.

    Parses OTEL traces into OpenAI conversation format, then uses KaizenClient
    to store the trajectory and generate guidelines.

    This function is best-effort — callers should wrap it in try/except.

    Args:
        traces_file: Path to traces/traces.jsonl
        agents_md_file: Path to AGENTS.md
        agent_output_file: Path to agent_output.json
        namespace_id: Kaizen namespace (derived from workspace if not provided)
        workspace_dir: Workspace directory (used to derive namespace if not provided)
        verbose: Enable verbose output
    """
    # Derive namespace from workspace path if not provided
    if not namespace_id and workspace_dir:
        namespace_id = _derive_kaizen_namespace(workspace_dir)
    if not namespace_id:
        namespace_id = "sre_default"

    # Parse traces into messages
    messages = parse_traces_to_messages(traces_file, agents_md_file, agent_output_file)

    if len(messages) < 2:
        if verbose:
            print("Kaizen: No meaningful trajectory to save (too few messages)", file=sys.stderr)
        return

    if verbose:
        print(f"Kaizen: Saving trajectory with {len(messages)} messages to namespace '{namespace_id}'")

    # Import Kaizen components (lazy import to avoid hard dependency at module level)
    import os

    from kaizen.config.kaizen import KaizenConfig
    from kaizen.frontend.client.kaizen_client import KaizenClient
    from kaizen.llm.tips.tips import generate_tips
    from kaizen.schema.core import Entity
    from kaizen.schema.exceptions import NamespaceNotFoundException

    # Determine backend from environment (default: milvus to match config.toml)
    backend = os.environ.get("KAIZEN_BACKEND", "milvus")

    # Determine kaizen_data dir (same logic as config.py)
    project_root = Path(__file__).parent.parent
    kaizen_data_dir = project_root / "kaizen_data"
    kaizen_data_dir.mkdir(parents=True, exist_ok=True)

    if backend == "filesystem":
        from kaizen.config.filesystem import FilesystemSettings

        fs_settings = FilesystemSettings(data_dir=str(kaizen_data_dir))
        config = KaizenConfig(backend="filesystem", namespace_id=namespace_id, settings=fs_settings)
    else:
        # Milvus backend — set URI to stable project-level location if not already set
        milvus_uri = str(kaizen_data_dir / "kaizen.milvus.db")
        os.environ.setdefault("KAIZEN_URI", milvus_uri)
        config = KaizenConfig(backend="milvus", namespace_id=namespace_id)

    client = KaizenClient(config=config)

    # Ensure namespace exists
    try:
        client.get_namespace_details(namespace_id)
    except NamespaceNotFoundException:
        client.create_namespace(namespace_id)

    # Generate a task ID for this trajectory
    task_id = str(uuid.uuid4())

    # Store raw trajectory entities
    trajectory_entities = [
        Entity(
            type="trajectory",
            content=msg["content"] if isinstance(msg["content"], str) else json.dumps(msg["content"]),
            metadata={
                "task_id": task_id,
                "message": msg,
            },
        )
        for msg in messages
    ]

    client.update_entities(
        namespace_id=namespace_id,
        entities=trajectory_entities,
        enable_conflict_resolution=False,
    )

    if verbose:
        print(f"Kaizen: Stored {len(trajectory_entities)} trajectory entities")

    # Generate tips from the trajectory
    try:
        tips = generate_tips(messages)
    except Exception as e:
        if verbose:
            print(f"Kaizen: Tip generation failed (non-fatal): {e}", file=sys.stderr)
        tips = []

    if tips:
        guideline_entities = [
            Entity(
                type="guideline",
                content=tip.content,
                metadata={
                    "category": tip.category,
                    "rationale": tip.rationale,
                    "trigger": tip.trigger,
                },
            )
            for tip in tips
        ]

        client.update_entities(
            namespace_id=namespace_id,
            entities=guideline_entities,
            enable_conflict_resolution=True,
        )

        if verbose:
            print(f"Kaizen: Generated and stored {len(tips)} guidelines")
    elif verbose:
        print("Kaizen: No tips generated from trajectory")

"""
Kaizen agentic memory integration for Zero.

Handles post-run trajectory saving: parses Codex exec output (stdout.log)
into OpenAI conversation format, and saves to Kaizen for tip generation
and guideline learning.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from .config import _derive_kaizen_namespace

# Maximum characters for tool result / reasoning content in trajectory messages.
# Keeps the trajectory manageable for tip generation without losing context.
_MAX_CONTENT_LEN = 4000


def parse_traces_to_messages(
    traces_file: Path,
    agents_md_file: Path,
    agent_output_file: Path | None = None,
    stdout_log: Path | None = None,
) -> list[dict]:
    """Parse Codex execution output into OpenAI-format conversation messages.

    Primary source is stdout.log (Codex --json exec output) which contains
    agent reasoning, tool calls, and results.  Falls back to traces.jsonl
    (OTEL logs) and finally to AGENTS.md + agent_output.json.

    Args:
        traces_file: Path to traces/traces.jsonl (legacy, secondary fallback)
        agents_md_file: Path to AGENTS.md (the substituted prompt)
        agent_output_file: Optional path to agent_output.json
        stdout_log: Path to traces/stdout.log (Codex --json exec output)

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

    # Primary: parse stdout.log (Codex --json exec output)
    if stdout_log:
        stdout_messages = _extract_messages_from_stdout_log(stdout_log)
        if stdout_messages:
            messages.extend(stdout_messages)
            return messages

    # Secondary fallback: parse OTEL traces
    trace_messages = _extract_messages_from_traces(traces_file)
    if trace_messages:
        messages.extend(trace_messages)
        return messages

    # Final fallback: use agent_output as a single response
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


def _truncate(text: str, max_len: int = _MAX_CONTENT_LEN) -> str:
    """Truncate text to max_len, appending '...' if trimmed."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ---------------------------------------------------------------------------
# Primary parser: Codex --json exec stdout.log
# ---------------------------------------------------------------------------

def _extract_messages_from_stdout_log(stdout_log: Path) -> list[dict]:
    """Extract conversation messages from Codex --json exec output.

    The stdout.log contains JSON lines emitted by ``codex exec --json``.
    Each line has a ``type`` field.  We care about ``item.completed`` events
    whose ``item`` contains the agent's reasoning or tool-call details.

    Item types we handle:
        agent_message   → assistant reasoning text
        mcp_tool_call   → function call + tool result
        command_execution → shell command + output
    """
    if not stdout_log or not stdout_log.exists():
        return []

    messages: list[dict] = []

    try:
        with open(stdout_log) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if record.get("type") != "item.completed":
                    continue

                item = record.get("item", {})
                item_type = item.get("type", "")

                if item_type == "agent_message":
                    msg = _agent_message_to_msg(item)
                    if msg:
                        messages.append(msg)

                elif item_type == "mcp_tool_call":
                    call_msg, result_msg = _mcp_tool_call_to_msgs(item)
                    if call_msg:
                        messages.append(call_msg)
                    if result_msg:
                        messages.append(result_msg)

                elif item_type == "command_execution":
                    call_msg, result_msg = _command_execution_to_msgs(item)
                    if call_msg:
                        messages.append(call_msg)
                    if result_msg:
                        messages.append(result_msg)

    except Exception:
        # Return whatever we managed to parse
        pass

    return messages


def _agent_message_to_msg(item: dict) -> dict | None:
    """Convert an agent_message item to an assistant message."""
    text = item.get("text", "")
    if not text or len(text.strip()) < 5:
        return None
    return {"role": "assistant", "content": _truncate(text)}


def _mcp_tool_call_to_msgs(item: dict) -> tuple[dict | None, dict | None]:
    """Convert an mcp_tool_call item to (function_call msg, tool result msg).

    Example item::

        {
            "type": "mcp_tool_call",
            "server": "kaizen",
            "tool": "get_guidelines",
            "arguments": {"task": "..."},
            "result": {"content": [{"type": "text", "text": "..."}]},
            "error": null,
            "status": "completed"
        }
    """
    server = item.get("server", "")
    tool = item.get("tool", "")
    tool_name = f"mcp__{server}__{tool}" if server else tool
    arguments = item.get("arguments", {})
    call_id = item.get("id", f"call_{tool_name}")

    args_str = json.dumps(arguments) if isinstance(arguments, dict) else str(arguments)

    call_msg: dict = {
        "role": "assistant",
        "content": [
            {
                "type": "function_call",
                "id": call_id,
                "function": {
                    "name": tool_name,
                    "arguments": _truncate(args_str),
                },
            }
        ],
    }

    # Build tool result
    result_text = ""
    result = item.get("result")
    error = item.get("error")

    if error:
        result_text = f"Error: {error}"
    elif result:
        # result.content is a list of {type, text} objects
        content_parts = result.get("content", [])
        if isinstance(content_parts, list):
            texts = [p.get("text", "") for p in content_parts if isinstance(p, dict)]
            result_text = "\n".join(texts)
        elif isinstance(result, str):
            result_text = result
        else:
            result_text = json.dumps(result)

    result_msg: dict | None = None
    if result_text:
        result_msg = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": _truncate(result_text),
        }

    return call_msg, result_msg


def _command_execution_to_msgs(item: dict) -> tuple[dict | None, dict | None]:
    """Convert a command_execution item to (function_call msg, tool result msg).

    Example item::

        {
            "type": "command_execution",
            "command": "/bin/zsh -c 'ls -lh ...'",
            "aggregated_output": "total 486320\\n...",
            "exit_code": 0,
            "status": "completed"
        }
    """
    command = item.get("command", "")
    call_id = item.get("id", "call_exec")

    args_str = json.dumps({"command": command})

    call_msg: dict = {
        "role": "assistant",
        "content": [
            {
                "type": "function_call",
                "id": call_id,
                "function": {
                    "name": "exec_command",
                    "arguments": _truncate(args_str),
                },
            }
        ],
    }

    output = item.get("aggregated_output", "")
    exit_code = item.get("exit_code")
    result_text = output
    if exit_code is not None and exit_code != 0:
        result_text = f"Exit code: {exit_code}\n{output}"

    result_msg: dict | None = None
    if result_text:
        result_msg = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": _truncate(result_text),
        }

    return call_msg, result_msg


# ---------------------------------------------------------------------------
# Legacy parser: OTEL traces.jsonl  (kept as secondary fallback)
# ---------------------------------------------------------------------------

def _extract_messages_from_traces(traces_file: Path) -> list[dict]:
    """Extract conversation messages from OTEL trace JSONL file.

    NOTE: As of Codex v0.98 the OTEL logs carry only metadata (event names,
    timing, token counts) with ``body: null``.  This parser is retained as a
    fallback but is unlikely to produce messages from current Codex versions.
    Prefer ``_extract_messages_from_stdout_log`` instead.
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
    """Convert a single OTEL log record to an OpenAI message format."""
    body = log_record.get("body", "")
    attributes = log_record.get("attributes", {})

    if not body and not attributes:
        return None

    # Check for tool/function call indicators in attributes
    tool_name = attributes.get("codex.tool_name", "") or attributes.get("tool.name", "")
    tool_args = attributes.get("codex.tool_arguments", "") or attributes.get("tool.arguments", "")

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

    if isinstance(body, str) and body.strip():
        if len(body.strip()) < 5:
            return None
        return {"role": "assistant", "content": _truncate(body)}

    if isinstance(body, (dict, list)):
        content = json.dumps(body)
        if len(content) < 5:
            return None
        return {"role": "assistant", "content": _truncate(content)}

    return None


# ---------------------------------------------------------------------------
# Public: save trajectory + generate tips
# ---------------------------------------------------------------------------

def save_trajectory_to_kaizen(
    *,
    traces_file: Path,
    agents_md_file: Path,
    agent_output_file: Path,
    stdout_log: Path | None = None,
    namespace_id: str | None = None,
    workspace_dir: Path | None = None,
    verbose: bool = False,
) -> None:
    """Save the agent's trajectory to Kaizen for tip generation and learning.

    Parses Codex execution output into OpenAI conversation format, then uses
    KaizenClient to store the trajectory and generate guidelines.

    This function is best-effort — callers should wrap it in try/except.

    Args:
        traces_file: Path to traces/traces.jsonl (legacy fallback)
        agents_md_file: Path to AGENTS.md
        agent_output_file: Path to agent_output.json
        stdout_log: Path to traces/stdout.log (primary source)
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
    messages = parse_traces_to_messages(
        traces_file, agents_md_file, agent_output_file, stdout_log
    )

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

    # Generate tips from the trajectory (retry up to 3 times for transient errors)
    tips = []
    max_tip_retries = 3
    for attempt in range(1, max_tip_retries + 1):
        try:
            tips = generate_tips(messages)
            break  # success
        except Exception as e:
            if verbose:
                print(
                    f"Kaizen: Tip generation attempt {attempt}/{max_tip_retries} failed: {e}",
                    file=sys.stderr,
                )
            if attempt < max_tip_retries:
                import time
                time.sleep(2 * attempt)  # simple back-off: 2s, 4s
            else:
                if verbose:
                    print("Kaizen: Tip generation failed after all retries", file=sys.stderr)
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

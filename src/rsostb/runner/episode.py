"""The multi-turn loop for interactive tool-calling and agentic tasks."""
from __future__ import annotations

from typing import Any

from ..adapters.base import AdapterError, ModelAdapter
from ..sandbox.base import SandboxUnavailable
from .environments import ToolEnvironment
from .protocol import parse_turn, results_message

INVALID_REPLY = ('Your reply was not valid ({reason}). Reply with ONLY a JSON object: '
                 '{{"tool_calls": [...]}} or {{"final_answer": "..."}}.')
MAX_MESSAGE_CHARS = 8000


def run_episode(adapter: ModelAdapter, task, messages: list[dict[str, str]], env: ToolEnvironment, *,
                max_steps: int = 16, max_invalid: int = 3, gen_kwargs: dict[str, Any] | None = None,
                call=None) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, Any]]:
    """Returns (episode record, transcript, usage totals)."""
    gen_kwargs = dict(gen_kwargs or {})
    call = call or (lambda msgs, **kw: adapter.chat(msgs, **kw))
    transcript = [dict(m) for m in messages]
    usage = {"input_tokens": 0, "output_tokens": 0, "latency_seconds": 0.0, "turns": 0}
    invalid_turns = 0
    final_answer = None
    stopped = "max_steps"
    steps = int((task.data.get("environment") or {}).get("max_steps", max_steps))
    for step in range(steps):
        try:
            gen = call(transcript, task=task, step=step, **gen_kwargs)
        except AdapterError as exc:
            stopped = "adapter_error"
            usage["error"] = str(exc)[:500]
            break
        usage["turns"] += 1
        usage["latency_seconds"] += gen.latency
        for k in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"):
            v = (gen.usage or {}).get(k)
            if isinstance(v, int):
                usage["input_tokens" if k in ("input_tokens", "prompt_tokens") else "output_tokens"] += v
        transcript.append({"role": "assistant", "content": gen.text[:MAX_MESSAGE_CHARS]})
        kind, payload = parse_turn(gen.text)
        if kind == "invalid":
            invalid_turns += 1
            if invalid_turns > max_invalid:
                stopped = "invalid_limit"
                break
            transcript.append({"role": "user", "content": INVALID_REPLY.format(reason=payload)})
            continue
        if kind == "final":
            final_answer = payload
            stopped = "final_answer"
            break
        entries = []
        try:
            for c in payload:
                entries.append(env.call(c["name"], c.get("arguments") if isinstance(c.get("arguments"), dict) else {}))
        except SandboxUnavailable:
            stopped = "sandbox_unavailable"
            break
        transcript.append({"role": "user", "content": results_message(entries)[:MAX_MESSAGE_CHARS]})
    state = env.state()
    if "files" in state:
        # Keep only changed files; the initial tree is part of the task definition.
        state = dict(state)
        state["files"] = {p: state["files"][p] for p in state.get("changed", []) if p in state["files"]}
        state["deleted"] = []
    episode = {
        "calls": [{k: v for k, v in e.items() if k != "result"} | {"result_preview": str(e.get("result"))[:300]}
                  for e in env.log],
        "invalid_turns": invalid_turns,
        "final_answer": final_answer,
        "turns": usage["turns"],
        "stopped": stopped,
        "env_state": state,
    }
    return episode, transcript, usage


def full_env_state(task, episode: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the final repository tree from the task's initial files plus the
    changed files recorded in the episode."""
    state = dict(episode.get("env_state") or {})
    if (task.data.get("environment") or {}).get("kind") == "repo":
        files = dict((task.data.get("environment") or {}).get("files") or {})
        files.update(state.get("files") or {})
        state["files"] = files
    return state

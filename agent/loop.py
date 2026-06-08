"""
The ReAct loop — multi-provider edition.

Sends the alert to the configured model with the read-only tool set, executes
tool calls, feeds results back, and repeats until the model produces a final
report or we hit a step limit.

Provider is selected by MODEL_PROVIDER env var, or inferred from MODEL prefix:
  claude-*           -> anthropic
  gpt-* / o1* / o3*  -> openai
  gemini-*           -> gemini
  anything else      -> ollama

Set MODEL (and optionally MODEL_PROVIDER) in .env to switch providers.
"""
from __future__ import annotations

import os

from agent.prompts import SYSTEM_PROMPT
from agent.providers import ModelTurn, provider_for_model, get_provider
from agent.skills import classify_skill, skill_prompt
from tools.registry import TOOL_SCHEMAS, dispatch
from tools.scrubber import safe_output

# Resolved at import time so preflight can read it.
MODEL = os.environ.get("MODEL", "claude-opus-4-8")

MAX_STEPS = 30
MAX_TOKENS = 4096


def _provider(model: str) -> str:
    """Return the provider name for *model*. Kept for main.py compatibility."""
    return provider_for_model(model)


def _print_step(label: str, body: str = "") -> None:
    print(f"\n{'─' * 70}\n{label}\n{'─' * 70}")
    if body:
        print(body)


def _fmt_input(d) -> str:
    if isinstance(d, dict):
        return "\n".join(f"  {k}: {v}" for k, v in d.items())
    return f"  {d}"


def _indent(text: str, n: int = 4) -> str:
    pad = " " * n
    return "\n".join(pad + line for line in text.splitlines()[:60])


def investigate(
    alert: str,
    cfg: dict,
    verbose: bool = True,
    recorder=None,
    selected_skill: str | None = None,
) -> str:
    """Run the investigation loop. Returns the model's final report text."""
    model = MODEL
    prov_name = _provider(model)
    provider = get_provider(prov_name)
    skill = selected_skill or classify_skill(alert)
    effective_system_prompt = SYSTEM_PROMPT + "\n\n" + skill_prompt(skill)
    namespace_scope = cfg.get("namespace_scope")
    if namespace_scope:
        effective_system_prompt += (
            "\n\n## Namespace scope\n"
            f"The requested namespace scope is `{namespace_scope}`. Limit namespaced "
            "Kubernetes and log investigation to this namespace. Do not try other "
            "namespaces unless the alert explicitly asks for a cross-namespace "
            "investigation or evidence from this namespace proves that a cluster-level "
            "dependency must be checked."
        )

    messages: list[dict] = [{"role": "user", "content": f"ALERT: {alert}"}]

    for step in range(1, MAX_STEPS + 1):

        # ── call the model ──────────────────────────────────────────────────
        turn: ModelTurn = provider.call(
            model=model,
            system_prompt=effective_system_prompt,
            messages=messages,
            tool_schemas=TOOL_SCHEMAS,
            max_tokens=MAX_TOKENS,
        )

        # ── surface reasoning ───────────────────────────────────────────────
        if turn.reasoning.strip():
            if verbose:
                _print_step(
                    f"[step {step}] reasoning ({prov_name}/{model})",
                    safe_output(turn.reasoning.strip()),
                )
            if recorder:
                recorder.add_model_message(turn.reasoning)

        provider.append_assistant_turn(messages, turn)

        if turn.done:
            final = safe_output(turn.reasoning)
            if recorder:
                recorder.set_final_answer(final)
                recorder.save()
            return final

        # ── execute tool calls ──────────────────────────────────────────────
        results: list[tuple] = []
        for tc in turn.tool_calls:
            if verbose:
                _print_step(f"[step {step}] tool: {tc.name}", _fmt_input(tc.input))
            if recorder:
                recorder.add_tool_call(tc.name, tc.input, safety_class="READ")

            try:
                observation = dispatch(tc.name, tc.input, cfg)
                observation = safe_output(observation)
            except Exception as exc:
                observation = f"[tool error] {exc}"
                if recorder:
                    recorder.add_tool_result(tc.name, observation, ok=False)
                raise

            if recorder:
                recorder.add_tool_result(tc.name, observation, ok=True)

            if verbose:
                print(f"\n  observation:\n{_indent(observation)}")

            results.append((tc, observation))

        provider.append_tool_results(messages, results)

    msg = (
        "[investigation hit MAX_STEPS without a conclusion — "
        "widen limits or refine the alert]"
    )
    if recorder:
        recorder.set_final_answer(msg)
        recorder.save()
    return msg

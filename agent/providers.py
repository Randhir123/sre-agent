"""
Provider abstraction for the SRE agent ReAct loop.

Each provider exposes:
  call(*, model, system_prompt, messages, tool_schemas, max_tokens) -> ModelTurn
  append_assistant_turn(messages, turn) -> None
  append_tool_results(messages, results) -> None

Provider selection:
  1. MODEL_PROVIDER env var (wins if set)
  2. Inferred from MODEL name prefix

Supported providers:
  openai            – OpenAI hosted models (gpt-*, o1/o3/o4)
  anthropic         – Anthropic hosted models (claude-*)
  gemini            – Google Gemini (gemini-*), needs GOOGLE_API_KEY or GEMINI_API_KEY
  ollama            – Local Ollama models, JSON tool protocol
  openai-compatible – Any OpenAI-compatible endpoint (vLLM, LM Studio, NIM, etc.)
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ModelTurn:
    reasoning: str
    done: bool
    tool_calls: list[ToolCall]
    # Provider-native assistant message — used by append_assistant_turn.
    # Shape differs per provider; loop.py never inspects this field.
    assistant_message: Any = None
    raw: Any = None


# ── Provider resolution ────────────────────────────────────────────────────────

def provider_for_model(model: str) -> str:
    """
    Return the provider name for *model*.

    MODEL_PROVIDER env var wins; otherwise inferred from model-name prefix.
    """
    explicit = os.environ.get("MODEL_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("gemini-"):
        return "gemini"
    return "ollama"


def get_provider(provider_name: str):
    """Return a freshly constructed provider for *provider_name*."""
    table: dict[str, type] = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "gemini": GeminiProvider,
        "ollama": OllamaProvider,
        "openai-compatible": OpenAICompatibleProvider,
    }
    cls = table.get(provider_name)
    if cls is None:
        raise ValueError(
            f"Unknown provider {provider_name!r}. "
            f"Valid choices: {', '.join(table)}"
        )
    return cls()


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _looks_like_final_report(text: str) -> bool:
    """Return True if *text* looks like a plain-text SRE incident report."""
    upper = text.upper()
    return any(marker in upper for marker in (
        "ROOT CAUSE",
        "EVIDENCE",
        "SUGGESTED FIX",
        "REMEDIATION",
        "VERIFICATION",
        "PREVENTION",
        "RULED OUT",
    ))


def _parse_json_loose(text: str) -> dict | None:
    """Extract the first JSON object from *text*; handle markdown fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ── AnthropicProvider ──────────────────────────────────────────────────────────

class AnthropicProvider:
    def __init__(self) -> None:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError(
                "anthropic package not installed. Run: pip install anthropic"
            )
        self._client = anthropic.Anthropic()

    def call(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tool_schemas: list[dict],
        max_tokens: int,
    ) -> ModelTurn:
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=tool_schemas,
            messages=messages,
        )
        reasoning = "".join(b.text for b in resp.content if b.type == "text")
        done = resp.stop_reason != "tool_use"
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in resp.content
            if b.type == "tool_use"
        ]
        return ModelTurn(
            reasoning=reasoning,
            done=done,
            tool_calls=tool_calls,
            assistant_message=resp.content,
        )

    def append_assistant_turn(self, messages: list, turn: ModelTurn) -> None:
        messages.append({"role": "assistant", "content": turn.assistant_message})

    def append_tool_results(
        self, messages: list, results: list[tuple[ToolCall, str]]
    ) -> None:
        if results:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tc.id, "content": obs}
                    for tc, obs in results
                ],
            })


# ── OpenAIProvider ─────────────────────────────────────────────────────────────

class OpenAIProvider:
    def __init__(self) -> None:
        try:
            import openai
        except ImportError:
            raise RuntimeError(
                "openai package not installed. Run: pip install openai"
            )
        self._client = openai.OpenAI()

    @staticmethod
    def _to_openai_tools(schemas: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": s["name"],
                    "description": s["description"],
                    "parameters": s["input_schema"],
                },
            }
            for s in schemas
        ]

    def _create(self, model: str, max_tokens: int, tools: list, messages: list):
        """Make the chat-completions call. Subclasses may override."""
        return self._client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,
            tools=tools,
            messages=messages,
        )

    def call(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tool_schemas: list[dict],
        max_tokens: int,
    ) -> ModelTurn:
        oai_msgs = [{"role": "system", "content": system_prompt}] + messages
        resp = self._create(model, max_tokens, self._to_openai_tools(tool_schemas), oai_msgs)
        msg = resp.choices[0].message
        reasoning = msg.content or ""
        done = resp.choices[0].finish_reason != "tool_calls"
        tool_calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                input=json.loads(tc.function.arguments),
            )
            for tc in (msg.tool_calls or [])
        ]
        return ModelTurn(
            reasoning=reasoning,
            done=done,
            tool_calls=tool_calls,
            assistant_message={
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])],
            },
        )

    def append_assistant_turn(self, messages: list, turn: ModelTurn) -> None:
        messages.append(turn.assistant_message)

    def append_tool_results(
        self, messages: list, results: list[tuple[ToolCall, str]]
    ) -> None:
        for tc, obs in results:
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": obs})


# ── GeminiProvider ─────────────────────────────────────────────────────────────

class GeminiProvider:
    """
    Google Gemini via the google-genai SDK with native function calling.

    The shared *messages* list in loop.py is not suitable for Gemini's native
    Content/Part objects, so this provider maintains its own _contents list
    and writes lightweight shadow entries into the shared messages list (for
    tracing only — not used in subsequent API calls).
    """

    def __init__(self) -> None:
        try:
            from google import genai  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "google-genai package not installed. Run: pip install google-genai"
            )
        api_key = (
            os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GEMINI_API_KEY", "")
        )
        if not api_key:
            raise RuntimeError(
                "Gemini requires GOOGLE_API_KEY or GEMINI_API_KEY to be set."
            )
        from google import genai as _genai
        self._client = _genai.Client(api_key=api_key)
        self._contents: list = []
        self._tools: list | None = None
        self._system_prompt: str = ""

    def _make_tools(self, tool_schemas: list[dict]):
        from google.genai import types
        return [types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=s["name"],
                description=s["description"],
                parameters=s["input_schema"],
            )
            for s in tool_schemas
        ])]

    def call(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tool_schemas: list[dict],
        max_tokens: int,
    ) -> ModelTurn:
        from google.genai import types

        if not self._contents:
            self._system_prompt = system_prompt
            self._tools = self._make_tools(tool_schemas)
            for msg in messages:
                if msg["role"] == "user" and isinstance(msg.get("content"), str):
                    self._contents.append(
                        types.Content(
                            role="user",
                            parts=[types.Part(text=msg["content"])],
                        )
                    )

        resp = self._client.models.generate_content(
            model=model,
            contents=self._contents,
            config=types.GenerateContentConfig(
                system_instruction=self._system_prompt,
                tools=self._tools,
                max_output_tokens=max_tokens,
            ),
        )

        candidate = resp.candidates[0]
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for part in candidate.content.parts:
            if getattr(part, "text", None):
                reasoning_parts.append(part.text)
            fc = getattr(part, "function_call", None)
            if fc is not None:
                call_id = getattr(fc, "id", None) or f"gem-{uuid.uuid4().hex[:8]}"
                tool_calls.append(ToolCall(
                    id=call_id,
                    name=fc.name,
                    input=dict(fc.args) if fc.args else {},
                ))

        reasoning = "".join(reasoning_parts)
        done = len(tool_calls) == 0

        return ModelTurn(
            reasoning=reasoning,
            done=done,
            tool_calls=tool_calls,
            assistant_message=candidate.content,
        )

    def append_assistant_turn(self, messages: list, turn: ModelTurn) -> None:
        self._contents.append(turn.assistant_message)
        messages.append({
            "role": "assistant",
            "content": turn.reasoning or "[tool_call]",
        })

    def append_tool_results(
        self, messages: list, results: list[tuple[ToolCall, str]]
    ) -> None:
        from google.genai import types
        parts = [
            types.Part(function_response=types.FunctionResponse(
                name=tc.name,
                response={"result": obs},
            ))
            for tc, obs in results
        ]
        self._contents.append(types.Content(role="user", parts=parts))
        for tc, obs in results:
            messages.append({"role": "tool", "content": obs})


# ── OllamaProvider ─────────────────────────────────────────────────────────────

_OLLAMA_TOOL_INSTRUCTIONS = """

## Available Tools

{tools_json}

## Response Format

# Ollama/local models often fail JSON escaping for long final reports, so we
# enforce JSON only for tool calls and allow plain-text final reports.

When you need to call a tool, respond with EXACTLY ONE JSON object and nothing else:
{{"tool": "tool_name", "args": {{...arguments...}}}}

When you have gathered enough information and are ready to give your final
incident report, write it as plain text — no JSON wrapper required.
Your plain-text report should include: evidence, root cause, confidence,
remediation commands (for human review only), and verification steps.

Do NOT wrap the final report in JSON. Only tool calls use JSON."""


class OllamaProvider:
    """
    Local Ollama models via the /api/chat endpoint.

    Uses a strict JSON tool-call protocol embedded in the system prompt because
    local models do not reliably support native function calling.
    """

    def __init__(self) -> None:
        import requests as _req
        self._req = _req
        self._base_url = os.environ.get(
            "OLLAMA_BASE_URL", "http://localhost:11434"
        ).rstrip("/")

    def _build_system(self, system_prompt: str, tool_schemas: list[dict]) -> str:
        tools_json = json.dumps(
            [
                {
                    "name": s["name"],
                    "description": s["description"],
                    "parameters": s["input_schema"],
                }
                for s in tool_schemas
            ],
            indent=2,
        )
        return system_prompt + _OLLAMA_TOOL_INSTRUCTIONS.format(tools_json=tools_json)

    def _to_ollama_messages(
        self, augmented_system: str, messages: list[dict]
    ) -> list[dict]:
        result = [{"role": "system", "content": augmented_system}]
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                result.append({"role": m.get("role", "user"), "content": content})
        return result

    def _chat(self, model: str, ollama_msgs: list[dict]) -> str:
        resp = self._req.post(
            f"{self._base_url}/api/chat",
            json={
                "model": model,
                "messages": ollama_msgs,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=600,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    def call(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tool_schemas: list[dict],
        max_tokens: int,
    ) -> ModelTurn:
        aug_system = self._build_system(system_prompt, tool_schemas)
        ollama_msgs = self._to_ollama_messages(aug_system, messages)

        content = self._chat(model, ollama_msgs)
        parsed = _parse_json_loose(content)

        # ── Tool call (strict JSON required) ───────────────────────────────
        if parsed is not None and "tool" in parsed:
            args = parsed.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            tc = ToolCall(
                id=f"olla-{uuid.uuid4().hex[:8]}",
                name=str(parsed["tool"]),
                input=args,
            )
            return ModelTurn(
                reasoning="",
                done=False,
                tool_calls=[tc],
                assistant_message={"role": "assistant", "content": content},
            )

        # ── {"final":"..."} wrapper (backward compatibility) ───────────────
        if parsed is not None and "final" in parsed:
            return ModelTurn(reasoning=str(parsed["final"]), done=True, tool_calls=[])

        # ── Plain-text final report (local models escape poorly in JSON) ───
        if parsed is None and _looks_like_final_report(content):
            return ModelTurn(reasoning=content, done=True, tool_calls=[])

        # ── JSON with neither "tool" nor "final" — treat as final ──────────
        if parsed is not None:
            return ModelTurn(reasoning=content, done=True, tool_calls=[])

        # ── parsed is None and doesn't look like a final report yet ────────
        # One repair attempt: ask for tool JSON or plain-text final report.
        repair_msgs = ollama_msgs + [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": (
                    "If you need to call a tool, return exactly one JSON object:\n"
                    '{"tool":"tool_name","args":{...}}\n'
                    "If you have finished your investigation, write the final "
                    "incident report as plain text."
                ),
            },
        ]
        content = self._chat(model, repair_msgs)
        parsed = _parse_json_loose(content)

        if parsed is not None and "tool" in parsed:
            args = parsed.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            tc = ToolCall(
                id=f"olla-{uuid.uuid4().hex[:8]}",
                name=str(parsed["tool"]),
                input=args,
            )
            return ModelTurn(
                reasoning="",
                done=False,
                tool_calls=[tc],
                assistant_message={"role": "assistant", "content": content},
            )

        if parsed is not None and "final" in parsed:
            return ModelTurn(reasoning=str(parsed["final"]), done=True, tool_calls=[])

        if _looks_like_final_report(content):
            return ModelTurn(reasoning=content, done=True, tool_calls=[])

        return ModelTurn(
            reasoning=(
                "[ollama: malformed JSON after repair attempt]\n"
                + content[:500]
            ),
            done=True,
            tool_calls=[],
        )

    def append_assistant_turn(self, messages: list, turn: ModelTurn) -> None:
        if turn.assistant_message:
            messages.append(turn.assistant_message)
        else:
            messages.append({"role": "assistant", "content": turn.reasoning})

    def append_tool_results(
        self, messages: list, results: list[tuple[ToolCall, str]]
    ) -> None:
        for tc, obs in results:
            messages.append({
                "role": "user",
                "content": f"Tool result for {tc.name}:\n{obs}",
            })


# ── OpenAICompatibleProvider ───────────────────────────────────────────────────

class OpenAICompatibleProvider(OpenAIProvider):
    """
    Any OpenAI-compatible chat-completions endpoint.

    Covers: NVIDIA NIM / Nemotron, vLLM, LM Studio, Together, Groq, Fireworks,
    OpenRouter, and others.

    Environment variables:
      OPENAI_COMPATIBLE_BASE_URL   base URL of the endpoint (required)
      OPENAI_COMPATIBLE_API_KEY    API key (optional; defaults to "dummy")
    """

    def __init__(self) -> None:
        try:
            import openai
        except ImportError:
            raise RuntimeError(
                "openai package not installed. Run: pip install openai"
            )

        base_url = os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "").rstrip("/")
        if not base_url:
            raise RuntimeError("OPENAI_COMPATIBLE_BASE_URL is not set.")
        if not base_url.endswith("/v1"):
            base_url += "/v1"

        api_key = os.environ.get("OPENAI_COMPATIBLE_API_KEY", "dummy")
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url)

    def _create(self, model: str, max_tokens: int, tools: list, messages: list):
        import openai
        try:
            # Use max_tokens (broader compatibility than max_completion_tokens)
            return self._client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                tools=tools,
                messages=messages,
            )
        except openai.BadRequestError as exc:
            if any(kw in str(exc).lower() for kw in ("tool", "function")):
                raise RuntimeError(
                    "This OpenAI-compatible endpoint rejected the tools parameter. "
                    "The endpoint may not support function calling. "
                    "Try MODEL_PROVIDER=ollama, or enable tool support on the server "
                    "(e.g. vLLM --enable-auto-tool-choice)."
                ) from exc
            raise

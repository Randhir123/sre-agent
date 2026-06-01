"""
TrajectoryRecorder — captures a full SRE agent investigation for offline eval.

Written files:
  evals/runs/<scenario_id>/<provider>/<safe_model>/<run_id>/trajectory.json
  evals/runs/<scenario_id>/<provider>/<safe_model>/<run_id>/raw/tool_result_NNN_<tool>.txt

No secrets are written: values of known secret env vars and suspicious dict
keys are replaced with [REDACTED] before anything is persisted.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import uuid
from typing import Any


# Env vars whose current values are scrubbed from all persisted text
_SECRET_ENV_VARS = [
    "IBM_CLOUD_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "LOCAL_LLM_API_KEY",
]

# Dict keys (lowercased, underscored) whose values are always redacted
_SECRET_DICT_KEYS = frozenset({
    "token", "apikey", "api_key", "password", "secret",
    "authorization", "bearer",
})

_REDACTED = "[REDACTED]"


# ── Redaction helpers ──────────────────────────────────────────────────────────

def _collect_secret_values() -> list[str]:
    vals = []
    for var in _SECRET_ENV_VARS:
        v = os.environ.get(var, "")
        if v and len(v) > 4:  # ignore trivially short placeholder values
            vals.append(v)
    return vals


def _scrub_str(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, _REDACTED)
    return text


def _scrub_dict(d: dict, secrets: list[str]) -> dict:
    out: dict = {}
    for k, v in d.items():
        key_norm = k.lower().replace("-", "_")
        if any(kw in key_norm for kw in _SECRET_DICT_KEYS):
            out[k] = _REDACTED
        elif isinstance(v, str):
            out[k] = _scrub_str(v, secrets)
        elif isinstance(v, dict):
            out[k] = _scrub_dict(v, secrets)
        elif isinstance(v, list):
            out[k] = [_scrub(item, secrets) for item in v]
        else:
            out[k] = v
    return out


def _scrub(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return _scrub_str(value, secrets)
    if isinstance(value, dict):
        return _scrub_dict(value, secrets)
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
    return value


# ── Misc helpers ───────────────────────────────────────────────────────────────

def _safe_name(s: str) -> str:
    """Replace characters unsafe for directory names."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", s)


def _now_iso() -> str:
    return (
        datetime.datetime.now(tz=datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


# ── TrajectoryRecorder ─────────────────────────────────────────────────────────

class TrajectoryRecorder:
    """
    Record every step of a ReAct investigation into a JSON trajectory file.

    Usage (from loop.py)::

        recorder.add_model_message(reasoning)
        recorder.add_tool_call(name, args, safety_class="READ")
        recorder.add_tool_result(name, observation, ok=True)
        recorder.set_final_answer(final_text)
        recorder.save()   # returns path; sets recorder.saved_path
    """

    def __init__(
        self,
        *,
        scenario_id: str,
        model: str,
        provider: str,
        alert: str,
        config: dict,
        out_dir: str = "evals/runs",
    ) -> None:
        self._secrets = _collect_secret_values()
        self._tool_result_counter = 0
        self.saved_path: str | None = None

        run_id = (
            datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        safe_model = _safe_name(model)
        run_dir = os.path.join(out_dir, scenario_id, provider, safe_model, run_id)
        self._raw_dir = os.path.join(run_dir, "raw")
        os.makedirs(self._raw_dir, exist_ok=True)
        self._trajectory_path = os.path.join(run_dir, "trajectory.json")

        self._data: dict = {
            "run_id": run_id,
            "scenario_id": scenario_id,
            "model": model,
            "provider": provider,
            "started_at": _now_iso(),
            "ended_at": None,
            "alert": _scrub_str(alert, self._secrets),
            "config": _scrub(config, self._secrets),
            "steps": [],
            "final_answer": None,
            "safety": {
                "mutation_attempted": False,
                "secret_exposed": False,
                "unsafe_recommendation": False,
            },
            "metrics": {
                "tool_calls": 0,
                "failed_tool_calls": 0,
                "malformed_tool_calls": 0,
                "duration_ms": None,
            },
        }

    # ── Step recording ─────────────────────────────────────────────────────────

    def add_model_message(self, content: str) -> None:
        self._data["steps"].append({
            "step_type": "model_message",
            "content": _scrub_str(content, self._secrets),
        })

    def add_tool_call(
        self, tool: str, args: dict, safety_class: str = "READ"
    ) -> None:
        self._data["steps"].append({
            "step_type": "tool_call",
            "tool": tool,
            "args": _scrub(args, self._secrets),
            "safety_class": safety_class,
        })
        self._data["metrics"]["tool_calls"] += 1

    def add_tool_result(self, tool: str, result: Any, ok: bool = True) -> None:
        if not ok:
            self._data["metrics"]["failed_tool_calls"] += 1

        self._tool_result_counter += 1
        idx = f"{self._tool_result_counter:03d}"
        raw_filename = f"tool_result_{idx}_{tool}.txt"
        raw_path = os.path.join(self._raw_dir, raw_filename)

        raw_text = _scrub_str(str(result), self._secrets)
        with open(raw_path, "w", encoding="utf-8") as fh:
            fh.write(raw_text)

        # Compact single-line summary for trajectory.json
        summary = raw_text[:800].replace("\n", " ").strip()

        self._data["steps"].append({
            "step_type": "tool_result",
            "tool": tool,
            "summary": summary,
            "raw_ref": f"raw/{raw_filename}",
            "ok": ok,
        })

    def set_final_answer(self, final_answer: str) -> None:
        self._data["final_answer"] = _scrub_str(final_answer, self._secrets)

    def mark_malformed_tool_call(self) -> None:
        self._data["metrics"]["malformed_tool_calls"] += 1

    # ── Persistence ────────────────────────────────────────────────────────────

    def save(self) -> str:
        """Write trajectory.json. Returns the file path and sets self.saved_path."""
        now = _now_iso()
        self._data["ended_at"] = now

        try:
            fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
            started = datetime.datetime.strptime(self._data["started_at"], fmt)
            ended = datetime.datetime.strptime(now, fmt)
            self._data["metrics"]["duration_ms"] = int(
                (ended - started).total_seconds() * 1000
            )
        except Exception:
            pass

        with open(self._trajectory_path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)

        self.saved_path = self._trajectory_path
        return self._trajectory_path

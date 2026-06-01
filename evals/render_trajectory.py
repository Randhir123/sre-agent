#!/usr/bin/env python3
"""
Render a trajectory.json into human-readable Markdown and a Mermaid diagram.

Usage:
  python evals/render_trajectory.py path/to/trajectory.json

Writes next to trajectory.json:
  trajectory.md   — full report with step table and embedded Mermaid diagram
  trajectory.mmd  — Mermaid diagram only (for direct rendering or copy-paste)

No external dependencies beyond the Python standard library.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _short(text: str, maxlen: int = 80) -> str:
    text = str(text).replace("\n", " ").strip()
    return (text[:maxlen] + "...") if len(text) > maxlen else text


def _duration_str(ms: int | None) -> str:
    if ms is None:
        return "?"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(int(s), 60)
    return f"{m}m {s}s"


def render(trajectory_path: str) -> None:
    path = Path(trajectory_path)
    with open(path, encoding="utf-8") as fh:
        t = json.load(fh)

    run_id = t.get("run_id", "?")
    scenario_id = t.get("scenario_id", "?")
    model = t.get("model", "?")
    provider = t.get("provider", "?")
    started = t.get("started_at", "?")
    ended = t.get("ended_at", "?")
    metrics = t.get("metrics", {})
    final_answer = t.get("final_answer", "") or ""
    steps = t.get("steps", [])

    # ── Mermaid diagram ────────────────────────────────────────────────────────
    # Collect tools used (in order of first appearance) for participant list
    tools_seen: list[str] = []
    for step in steps:
        if step.get("step_type") == "tool_call":
            tool = step.get("tool", "?")
            if tool not in tools_seen:
                tools_seen.append(tool)

    mmd: list[str] = ["sequenceDiagram", "    participant User", "    participant Agent"]
    for tool in tools_seen:
        mmd.append(f"    participant {tool}")

    mmd.append("    User->>Agent: alert")

    for step in steps:
        st = step.get("step_type", "")
        tool = step.get("tool", "")

        if st == "tool_call":
            args_summary = _short(step.get("args", {}), 55)
            mmd.append(f"    Agent->>{tool}: {args_summary}")

        elif st == "tool_result":
            result_summary = _short(step.get("summary", ""), 55)
            ok = step.get("ok", True)
            arrow = "--x>" if not ok else "-->>"
            mmd.append(f"    {tool}{arrow}Agent: {result_summary}")

    if final_answer:
        mmd.append(f"    Agent-->>User: {_short(final_answer, 55)}")

    mmd_text = "\n".join(mmd)

    # ── Step table ─────────────────────────────────────────────────────────────
    table_rows: list[str] = []
    for i, step in enumerate(steps, 1):
        st = step.get("step_type", "?")
        tool = step.get("tool", "")
        if st == "model_message":
            summary = _short(step.get("content", ""), 80)
        elif st == "tool_call":
            summary = _short(step.get("args", {}), 80)
        elif st == "tool_result":
            ok_marker = "" if step.get("ok", True) else " ❌"
            summary = _short(step.get("summary", ""), 78) + ok_marker
        else:
            summary = ""
        table_rows.append(f"| {i} | {st} | {tool} | {summary} |")

    table_md = "\n".join(
        ["| step | type | tool | summary |", "|------|------|------|---------|"]
        + table_rows
    )

    final_short = _short(final_answer, 400)

    # ── Markdown report ────────────────────────────────────────────────────────
    md = f"""# Trajectory: {run_id}

| Field | Value |
|-------|-------|
| scenario_id | `{scenario_id}` |
| model | `{model}` |
| provider | `{provider}` |
| started_at | `{started}` |
| ended_at | `{ended}` |
| duration | {_duration_str(metrics.get("duration_ms"))} |
| tool_calls | {metrics.get("tool_calls", 0)} |
| failed_tool_calls | {metrics.get("failed_tool_calls", 0)} |
| malformed_tool_calls | {metrics.get("malformed_tool_calls", 0)} |

## Final Answer (summary)

{final_short}

## Steps

{table_md}

## Sequence Diagram

```mermaid
{mmd_text}
```
"""

    md_path = path.parent / "trajectory.md"
    mmd_path = path.parent / "trajectory.mmd"

    md_path.write_text(md, encoding="utf-8")
    mmd_path.write_text(mmd_text + "\n", encoding="utf-8")

    print(f"Written: {md_path}")
    print(f"Written: {mmd_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python evals/render_trajectory.py path/to/trajectory.json")
        sys.exit(1)
    render(sys.argv[1])

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


ARTIFACT_FILES = {
    "report_md": "report.md",
    "report_json": "report.json",
    "bob_task": "bob-task.md",
    "validation_plan": "validation-plan.md",
    "dispatch": "dispatch.json",
}


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80].strip("-") or "incident"


def _incident_id(*, created_at: datetime, service: str | None, alert: str) -> str:
    slug_source = service or alert
    suffix = uuid.uuid4().hex[:8]
    timestamp = created_at.strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return f"{timestamp}-{_slugify(slug_source)}-{suffix}"


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _report_markdown(
    *,
    alert: str,
    namespace: str,
    service: str | None,
    final_report: str,
    model: str,
    provider: str,
) -> str:
    service_value = service or "unknown"
    return f"""# SRE Incident Report

## Alert

{alert}

## Namespace

{namespace}

## Service

{service_value}

## Model

{model} ({provider})

## Final SRE Report

{final_report}
"""


def _bob_task_markdown(
    *,
    alert: str,
    namespace: str,
    service: str | None,
    final_report: str,
) -> str:
    service_value = service or "unknown"
    return f"""# Bob Task: Runtime-to-Code Investigation

## Runtime alert

{alert}

## Namespace

{namespace}

## Service

{service_value}

## SRE incident report

{final_report}

## Instructions for Bob

You are the only coding agent allowed to inspect or modify the target repository.

The SRE agent has investigated runtime evidence only. Use the report above as context, but verify everything against the repository before proposing code changes.

### Phase 1 — Repository grounding

Do not edit files in this phase.

Inspect the repository and return:

1. Exact file paths inspected.
2. Exact classes, functions, methods, config keys, Helm values, or deployment templates related to the runtime symptom.
3. Whether the runtime hypothesis is supported by code/config evidence.
4. Whether the likely issue is code, config, dependency, runtime-only, or inconclusive.
5. The first exact code/config area you would change if approved.

Stop after Phase 1 and wait for approval.

### Phase 2 — Minimal patch plan

After Phase 1 approval, produce a minimal patch plan.

Return:

1. Minimal code-change plan.
2. Dependency impact.
3. Config/Helm/env impact.
4. Tests to run.
5. Risks and rollback notes.

Stop after Phase 2 and wait for approval.

### Phase 3 — Implementation

After approval:

1. Make the smallest safe patch.
2. Avoid unrelated refactors or formatting-only changes.
3. Add or update focused tests if practical.
4. Run the smallest relevant test command.
5. Summarize changed files, behavior change, and runtime validation steps.
"""


def _validation_plan_markdown(
    *,
    alert: str,
    namespace: str,
    service: str | None,
) -> str:
    service_value = service or "unknown"
    return f"""# Validation Plan

## Scope

- Alert: {alert}
- Namespace: {namespace}
- Service: {service_value}

## After Bob's fix

1. Deploy the approved Bob change through the normal release path.
2. Rerun the SRE investigation or a targeted validation for the same alert scope.
3. Compare logs, metrics, pod restarts, and error rates from before and after the change.
4. Confirm the original runtime symptom is reduced or gone over an appropriate observation window.
5. Record any remaining runtime evidence for follow-up.

Validation is external to Bob. Bob can propose validation steps, but runtime verification must be performed through the SRE workflow or other operational tooling.
"""


def write_incident_artifacts(
    *,
    alert: str,
    namespace: str,
    final_report: str,
    model: str,
    provider: str,
    config_path: str,
    incident_dir: str,
    service: str | None = None,
    target_repo: str | None = None,
) -> Path:
    created_at = datetime.now(timezone.utc)
    incident_id = _incident_id(created_at=created_at, service=service, alert=alert)

    root = Path(incident_dir).expanduser()
    incident_path = root / incident_id
    incident_path.mkdir(parents=True, exist_ok=False)

    paths = {key: incident_path / filename for key, filename in ARTIFACT_FILES.items()}

    _write_text(
        paths["report_md"],
        _report_markdown(
            alert=alert,
            namespace=namespace,
            service=service,
            final_report=final_report,
            model=model,
            provider=provider,
        ),
    )
    _write_text(
        paths["bob_task"],
        _bob_task_markdown(
            alert=alert,
            namespace=namespace,
            service=service,
            final_report=final_report,
        ),
    )
    _write_text(
        paths["validation_plan"],
        _validation_plan_markdown(alert=alert, namespace=namespace, service=service),
    )

    report_json = {
        "incident_id": incident_id,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "alert": alert,
        "namespace": namespace,
        "service": service,
        "model": model,
        "provider": provider,
        "config_path": config_path,
        "final_report": final_report,
        "artifact_files": ARTIFACT_FILES,
    }
    paths["report_json"].write_text(
        json.dumps(report_json, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    dispatch_json = {
        "incident_id": incident_id,
        "status": "ready_for_bob",
        "service": service,
        "namespace": namespace,
        "target_repo_path": target_repo,
        "bob_task_file": str(paths["bob_task"].resolve()),
        "report_file": str(paths["report_md"].resolve()),
        "validation_plan_file": str(paths["validation_plan"].resolve()),
    }
    paths["dispatch"].write_text(
        json.dumps(dispatch_json, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return incident_path

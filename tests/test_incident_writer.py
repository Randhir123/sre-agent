import json
import re

from sre_bridge.incident_writer import write_incident_artifacts


def test_write_incident_artifacts_creates_bob_handoff(tmp_path):
    incident_path = write_incident_artifacts(
        alert="time-series-query has read timeouts and socket growth",
        namespace="si",
        final_report="Runtime evidence points to read timeout growth.",
        model="gpt-5.5",
        provider="openai",
        config_path="config.yaml",
        incident_dir=str(tmp_path),
        service="time-series-query",
        target_repo="/path/to/work/repo",
    )

    assert re.match(
        r"^\d{8}-\d{6}-\d{3}-time-series-query-[a-f0-9]{8}$",
        incident_path.name,
    )
    assert sorted(path.name for path in incident_path.iterdir()) == [
        "bob-task.md",
        "dispatch.json",
        "report.json",
        "report.md",
        "validation-plan.md",
    ]

    bob_task = (incident_path / "bob-task.md").read_text(encoding="utf-8")
    assert "You are the only coding agent allowed to inspect or modify the target repository." in bob_task
    assert "The SRE agent has investigated runtime evidence only." in bob_task
    assert "Do not edit files in this phase." in bob_task
    assert "Stop after Phase 1 and wait for approval." in bob_task

    report = json.loads((incident_path / "report.json").read_text(encoding="utf-8"))
    assert report["alert"] == "time-series-query has read timeouts and socket growth"
    assert report["service"] == "time-series-query"
    assert report["artifact_files"] == {
        "report_md": "report.md",
        "report_json": "report.json",
        "bob_task": "bob-task.md",
        "validation_plan": "validation-plan.md",
        "dispatch": "dispatch.json",
    }

    dispatch = json.loads((incident_path / "dispatch.json").read_text(encoding="utf-8"))
    assert dispatch["status"] == "ready_for_bob"
    assert dispatch["target_repo_path"] == "/path/to/work/repo"
    assert dispatch["bob_task_file"] == str((incident_path / "bob-task.md").resolve())
    assert dispatch["report_file"] == str((incident_path / "report.md").resolve())
    assert dispatch["validation_plan_file"] == str((incident_path / "validation-plan.md").resolve())


def test_incident_slug_falls_back_to_alert_when_service_missing(tmp_path):
    incident_path = write_incident_artifacts(
        alert="HTTP 500s in API /checkout!",
        namespace="payments",
        final_report="No code action taken.",
        model="gemma3:12b",
        provider="ollama",
        config_path="config.yaml",
        incident_dir=str(tmp_path),
    )

    assert re.match(
        r"^\d{8}-\d{6}-\d{3}-http-500s-in-api-checkout-[a-f0-9]{8}$",
        incident_path.name,
    )
    dispatch = json.loads((incident_path / "dispatch.json").read_text(encoding="utf-8"))
    assert dispatch["target_repo_path"] is None


def test_incident_ids_are_collision_resistant(tmp_path):
    first = write_incident_artifacts(
        alert="same alert",
        namespace="si",
        final_report="first",
        model="gpt-5.5",
        provider="openai",
        config_path="config.yaml",
        incident_dir=str(tmp_path),
        service="same-service",
    )
    second = write_incident_artifacts(
        alert="same alert",
        namespace="si",
        final_report="second",
        model="gpt-5.5",
        provider="openai",
        config_path="config.yaml",
        incident_dir=str(tmp_path),
        service="same-service",
    )

    assert first.name != second.name

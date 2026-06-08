from tools import registry
from tools.runner import CommandResult


def test_dispatch_forwards_query_logs_arguments(monkeypatch):
    received = {}

    def fake_query_logs(**kwargs):
        received.update(kwargs)
        return "logs result"

    monkeypatch.setattr(registry, "_query_logs", fake_query_logs)

    result = registry.dispatch(
        "query_logs",
        {
            "query": "UNKNOWN_TOPIC_OR_PARTITION",
            "namespace": "si",
            "app": "multi-system-processor",
            "since_minutes": 120,
            "limit": 10,
        },
        {"default_namespace": "default"},
    )

    assert result == "logs result"
    assert received == {
        "query": "UNKNOWN_TOPIC_OR_PARTITION",
        "namespace": "si",
        "app": "multi-system-processor",
        "since_minutes": 120,
        "limit": 10,
    }


def test_dispatch_uses_query_logs_defaults(monkeypatch):
    received = {}

    def fake_query_logs(**kwargs):
        received.update(kwargs)
        return "logs result"

    monkeypatch.setattr(registry, "_query_logs", fake_query_logs)

    registry.dispatch(
        "query_logs",
        {"query": "rebalance"},
        {"default_namespace": "si"},
    )

    assert received == {
        "query": "rebalance",
        "namespace": "si",
        "app": None,
        "since_minutes": 60,
        "limit": 200,
    }


def test_dispatch_enforces_locked_namespace_for_query_logs(monkeypatch):
    received = {}

    def fake_query_logs(**kwargs):
        received.update(kwargs)
        return "logs result"

    monkeypatch.setattr(registry, "_query_logs", fake_query_logs)

    result = registry.dispatch(
        "query_logs",
        {"query": "restart", "namespace": "default"},
        {"default_namespace": "si", "namespace_scope": "si", "namespace_locked": True},
    )

    assert "namespace scope enforced" in result
    assert received["namespace"] == "si"


def test_dispatch_enforces_locked_namespace_for_kubectl_get(monkeypatch):
    received = {}

    def fake_run_readonly(cmd, timeout=60):
        received["cmd"] = cmd
        return CommandResult(ok=True, stdout="pods", stderr="")

    monkeypatch.setattr(registry, "run_readonly", fake_run_readonly)

    result = registry.dispatch(
        "kubectl_get",
        {"resource": "pods", "namespace": "default"},
        {"default_namespace": "si", "namespace_scope": "si", "namespace_locked": True},
    )

    assert "namespace scope enforced" in result
    assert received["cmd"] == "kubectl get pods -n si -o wide"

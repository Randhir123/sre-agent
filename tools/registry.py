"""
Tool registry.

Defines:
  - TOOL_SCHEMAS  : the JSON tool definitions sent to Claude
  - dispatch()    : maps a tool call to an actual read-only execution

Every tool here is READ-ONLY. Mutations are never tools — the agent only
*suggests* fix commands in its final report, for a human to run.
"""
from __future__ import annotations

from tools.runner import run_readonly
from tools.prometheus import Prometheus, summarize_result
from tools.ibm_logs import query_logs as _query_logs

# ---- Claude-facing tool schemas -------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "kubectl_get",
        "description": (
            "List Kubernetes resources (read-only). Use for pods, replicasets, "
            "deployments, events, nodes. Returns wide output. Example resource "
            "values: 'pods', 'rs', 'deployments', 'events'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "e.g. pods, rs, deployments, events"},
                "namespace": {"type": "string"},
                "label_selector": {"type": "string", "description": "optional, e.g. app=multi-system-processor"},
                "extra_flags": {"type": "string", "description": "optional read-only flags, e.g. '--sort-by=.lastTimestamp'"},
            },
            "required": ["resource", "namespace"],
        },
    },
    {
        "name": "kubectl_describe",
        "description": "Describe a Kubernetes resource for detailed status, events, restart counts, probe config (read-only).",
        "input_schema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "e.g. pod, deployment"},
                "namespace": {"type": "string"},
                "name": {"type": "string", "description": "resource name, OR omit and use label_selector"},
                "label_selector": {"type": "string"},
            },
            "required": ["resource", "namespace"],
        },
    },
    {
        "name": "query_logs",
        "description": (
            "Query IBM Cloud Logs (aggregated, persistent logs). PREFER THIS "
            "for investigation — these logs survive pod restarts, deployments, "
            "and scale-downs, and span all pod incarnations of a service. Use "
            "for any historical or timeline analysis. Search by plain-text keyword."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "plain-text keyword, e.g. UNKNOWN_TOPIC_OR_PARTITION"},
                "namespace": {"type": "string"},
                "app": {"type": "string", "description": "optional app/container name to scope to"},
                "since_minutes": {"type": "integer", "description": "look-back window in minutes, default 60"},
                "limit": {"type": "integer", "description": "max lines, default 200"},
            },
            "required": ["query", "namespace"],
        },
    },
    {
        "name": "kubectl_logs",
        "description": (
            "Fetch LIVE pod logs (read-only). LIMITED: only currently-running "
            "pods, lost when a pod terminates. Use only for current pod state or "
            "a quick 'what is this pod doing now' check — NOT historical analysis. "
            "For history use query_logs instead. Supports label selector, since "
            "window, and an optional grep pattern."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "label_selector": {"type": "string"},
                "pod": {"type": "string", "description": "specific pod name (alternative to label_selector)"},
                "since": {"type": "string", "description": "e.g. 30m, 1h, 2h. default 1h"},
                "grep": {"type": "string", "description": "optional regex/keyword to filter log lines"},
                "tail": {"type": "integer", "description": "max lines to return after filtering, default 100"},
            },
            "required": ["namespace"],
        },
    },
    {
        "name": "prometheus_query",
        "description": (
            "Run an instant PromQL query (read-only). Use to measure rebalance "
            "rates, restart counts, latency, saturation. Returns a compact summary "
            "of the resulting time series."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "promql": {"type": "string", "description": "the PromQL expression"},
            },
            "required": ["promql"],
        },
    },
    {
        "name": "ibmcloud_es",
        "description": (
            "Run a read-only IBM Event Streams CLI subcommand. Examples: "
            "'groups' (list consumer groups), 'group <id>' (describe a group's "
            "state, members, lag), 'topics' (list topics), 'topic <name>'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {"type": "string", "description": "the es subcommand and args, e.g. 'group reporting'"},
            },
            "required": ["args"],
        },
    },
]


# ---- Dispatch --------------------------------------------------------------

def _filter_and_tail(text: str, grep: str | None, tail: int) -> str:
    import re

    lines = text.splitlines()
    if grep:
        try:
            pat = re.compile(grep, re.IGNORECASE)
            lines = [ln for ln in lines if pat.search(ln)]
        except re.error:
            lines = [ln for ln in lines if grep.lower() in ln.lower()]
    if tail and len(lines) > tail:
        lines = lines[-tail:]
    return "\n".join(lines) if lines else "[no matching log lines]"


def _namespace_for(tool_input: dict, cfg: dict) -> tuple[str, str]:
    requested = tool_input.get("namespace")
    if cfg.get("namespace_locked") and cfg.get("namespace_scope"):
        scoped = cfg["namespace_scope"]
        if requested and requested != scoped:
            return scoped, f"[namespace scope enforced: requested '{requested}', using '{scoped}']\n"
        return scoped, ""
    return requested or cfg.get("default_namespace", ""), ""


def dispatch(tool_name: str, tool_input: dict, cfg: dict) -> str:
    """Execute a tool call and return a text observation for the LLM."""
    ns, namespace_note = _namespace_for(tool_input, cfg)

    if tool_name == "kubectl_get":
        cmd = f"kubectl get {tool_input['resource']} -n {ns} -o wide"
        if tool_input.get("label_selector"):
            cmd += f" -l {tool_input['label_selector']}"
        if tool_input.get("extra_flags"):
            cmd += f" {tool_input['extra_flags']}"
        return namespace_note + run_readonly(cmd).as_observation()

    if tool_name == "kubectl_describe":
        cmd = f"kubectl describe {tool_input['resource']} -n {ns}"
        if tool_input.get("name"):
            cmd += f" {tool_input['name']}"
        elif tool_input.get("label_selector"):
            cmd += f" -l {tool_input['label_selector']}"
        return namespace_note + run_readonly(cmd).as_observation()

    if tool_name == "kubectl_logs":
        since = tool_input.get("since", "1h")
        cmd = f"kubectl logs -n {ns} --since={since} --prefix=true"
        if tool_input.get("pod"):
            cmd += f" {tool_input['pod']}"
        elif tool_input.get("label_selector"):
            cmd += f" -l {tool_input['label_selector']}"
        result = run_readonly(cmd, timeout=90)
        if not result.ok and not result.stdout:
            return namespace_note + result.as_observation()
        return namespace_note + _filter_and_tail(
            result.stdout, tool_input.get("grep"), tool_input.get("tail", 100)
        )

    if tool_name == "query_logs":
        return namespace_note + _query_logs(
            query=tool_input["query"],
            namespace=ns,
            app=tool_input.get("app"),
            since_minutes=tool_input.get("since_minutes", 60),
            limit=tool_input.get("limit", 200),
        )

    if tool_name == "prometheus_query":
        prom = Prometheus(cfg["prometheus_url"])
        try:
            raw = prom.query(tool_input["promql"])
            return summarize_result(raw)
        except Exception as e:
            return f"[prometheus error] {e}"

    if tool_name == "ibmcloud_es":
        es_args = tool_input.get("args", "")
        if not es_args:
            return "[ibmcloud_es] missing args — specify a subcommand, e.g. 'groups' or 'group <id>'"
        cmd = f"ibmcloud es {es_args}"
        return run_readonly(cmd).as_observation()

    return f"[error] unknown tool: {tool_name}"

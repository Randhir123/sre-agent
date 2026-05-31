# SRE Agent

An autonomous SRE agent that investigates infrastructure incidents using a
ReAct (Reason + Act) loop powered by an LLM. It starts with Kafka consumer
rebalances but the tool layer is generic, so it extends to latency, OOM,
disk pressure, etc. without code changes.

## Design principles

- **Live but read-only.** The agent runs real `kubectl`, Prometheus, and log
  queries against your cluster. It can read anything. It executes nothing that
  mutates state.
- **Suggest, don't act.** When it finds a root cause it prints the fix commands
  for a human to review and run. No auto-execute.
- **Generic tools, smart reasoning.** Tools are primitives (`kubectl_get`,
  `prometheus_query`, `get_logs`). The domain knowledge (what a Kafka rebalance
  means, what `UNKNOWN_TOPIC_OR_PARTITION` implies) lives in the LLM, not in
  hardcoded if-statements.

## Safety model

Every tool is classified as `READ` or `MUTATE`. The executor refuses to run
anything classified `MUTATE`. The command allowlist is enforced before a
subprocess ever starts — the LLM cannot talk the executor into running
`kubectl delete`.

## Architecture

```
  alert ─▶ Agent (ReAct loop) ─▶ LLM (reason) ─▶ tool call
              ▲                                          │
              └──────────── tool result ◀───────────────┘

  Tools (all read-only):
    kubectl_get / kubectl_describe / kubectl_logs / kubectl_events
    prometheus_query
    ibmcloud_es (consumer group states)
    query_logs (IBM cloud logs)
```

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...
# Make sure kubectl context + prometheus URL in config.yaml are correct
python main.py --alert "Kafka consumer rebalances spiking in namespace si"
```

## Files

- `main.py` — entrypoint / CLI
- `agent/loop.py` — the ReAct loop
- `agent/prompts.py` — system prompt (the SRE brain)
- `tools/registry.py` — tool definitions + safety classification + dispatch
- `tools/kubectl.py` — kubectl wrappers (read-only)
- `tools/prometheus.py` — Prometheus HTTP client
- `tools/ibmcloud.py` — IBM Event Streams CLI wrapper
- `config.yaml` — per-environment config

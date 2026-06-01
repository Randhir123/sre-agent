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
cp .env.example .env   # fill in your keys
# Make sure kubectl context + prometheus URL in config.yaml are correct
python main.py --alert "Kafka consumer rebalances spiking in namespace si"
```

## Model providers

The agent supports five provider backends. Set `MODEL_PROVIDER` explicitly, or
let it be inferred from the `MODEL` name prefix (see `.env.example`).

**OpenAI**
```bash
MODEL_PROVIDER=openai MODEL=gpt-5.5 python main.py \
  --alert "Kafka consumer rebalances spiking in namespace si"
```

**Anthropic**
```bash
MODEL_PROVIDER=anthropic MODEL=claude-opus-4-8 python main.py \
  --alert "Kafka consumer rebalances spiking in namespace si"
```

**Gemini** (needs `GOOGLE_API_KEY` or `GEMINI_API_KEY`)
```bash
MODEL_PROVIDER=gemini MODEL=gemini-2.5-pro python main.py \
  --alert "Kafka consumer rebalances spiking in namespace si"
```

**Ollama local** (uses JSON tool-call protocol; works with Gemma, Llama, Qwen, etc.)
```bash
ollama pull gemma3:12b
MODEL_PROVIDER=ollama MODEL=gemma3:12b python main.py \
  --alert "Kafka consumer rebalances spiking in namespace si" --skip-preflight
```

**OpenAI-compatible** (NVIDIA NIM / Nemotron, vLLM, LM Studio, Together, Groq, Fireworks, OpenRouter …)
```bash
MODEL_PROVIDER=openai-compatible \
MODEL=<model-id> \
OPENAI_COMPATIBLE_BASE_URL=https://your-endpoint.example.com \
OPENAI_COMPATIBLE_API_KEY=<key> \
python main.py --alert "Kafka consumer rebalances spiking in namespace si"
```

## Trajectory capture

Record a full investigation for offline model evaluation:

```bash
MODEL_PROVIDER=openai MODEL=gpt-5.5 python main.py \
  --alert "Kafka consumer rebalances spiking in namespace si for multi-system-processor" \
  --namespace si \
  --record-trajectory \
  --scenario-id kafka_unknown_topic \
  --skip-preflight
```

Trajectories are written under `evals/runs/` (git-ignored):

```
evals/runs/<scenario_id>/<provider>/<model>/<run_id>/trajectory.json
evals/runs/<scenario_id>/<provider>/<model>/<run_id>/raw/
```

Score a run against the deterministic rubric:

```bash
python evals/verify_kafka_unknown_topic.py evals/runs/.../trajectory.json
```

Render a run to Markdown + Mermaid sequence diagram:

```bash
python evals/render_trajectory.py evals/runs/.../trajectory.json
# → trajectory.md and trajectory.mmd written next to trajectory.json
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

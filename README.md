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
  `prometheus_query`, `query_logs`). The domain knowledge (what a Kafka rebalance
  means, what a missing topic implies) lives in the LLM, not in hardcoded
  if-statements.

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
    kubectl_get / kubectl_describe / kubectl_logs
    prometheus_query
    ibmcloud_es (consumer group states)
    query_logs (IBM Cloud Logs)
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
# Make sure kubectl context + prometheus URL in config.yaml are correct
python main.py --alert "Kafka consumer rebalances spiking in namespace si"
```

### IBM Cloud, Kubernetes, and Prometheus access

Before running the agent, log in to IBM Cloud and target the appropriate
account and resource group. The SSO login command starts a browser-based
one-time passcode flow. Follow that flow and select the appropriate IBM Cloud
account when prompted.

```bash
ibmcloud login --sso
ibmcloud target -g <RESOURCE_GROUP_NAME_OR_ID>
```

Download the Kubernetes cluster configuration, then confirm that `kubectl`
uses the intended context and can reach the cluster:

```bash
ibmcloud ks cluster config --cluster <CLUSTER_NAME>
kubectl config current-context
kubectl get nodes
```

Start the Prometheus port-forward in a separate terminal and keep it running
while the agent runs:

```bash
kubectl port-forward svc/kube-prometheus-stack-prometheus 9090:9090 -n monitoring
```

Prometheus queries use the URL configured in `config.yaml`, commonly
`http://localhost:9090`. If `--skip-preflight` is used, the agent may skip some
checks, but Prometheus queries still need the port-forward when `config.yaml`
points to `localhost:9090`.

Run the agent from another terminal:

```bash
MODEL_PROVIDER=openai \
MODEL=gpt-5.5 \
python main.py \
  --alert "Kafka consumer rebalances are spiking in namespace si for multi-system-processor. Investigate using read-only tools and produce evidence, likely root cause, remediation suggestions, and verification steps." \
  --namespace si
```

The agent uses four distinct backend access paths:

- **Kubernetes `kubectl` access:** `kubectl` tools use the active kubeconfig
  context.
- **IBM Cloud Logs API access:** log queries call the IBM Cloud Logs API
  directly and do not require the Prometheus port-forward. Set
  `IBM_CLOUD_API_KEY` and `IBM_LOGS_ENDPOINT`.
- **IBM Event Streams CLI access:** Event Streams checks require the
  `ibmcloud` CLI to be logged in and targeted to the correct account and
  resource group.
- **Prometheus access:** Prometheus queries require the URL configured in
  `config.yaml` to be reachable, commonly through the local port-forward
  above.

#### Security and privacy

Do not commit `.env` files, API keys, bearer tokens, kubeconfig files, account
IDs, resource group IDs, cluster names, user emails, internal hostnames, or
internal URLs. For public documentation and blog posts, use placeholders such
as `<ACCOUNT>`, `<RESOURCE_GROUP_NAME_OR_ID>`, `<CLUSTER_NAME>`,
`<IBM_LOGS_ENDPOINT>`, and `<NAMESPACE>`.

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

## Running investigations vs evaluations

You do **not** need anything under `evals/` for normal SRE investigations.
Pass a fresh `--alert` describing the symptom and let the agent investigate.

The `evals/` directory is for **repeatable model comparison**: fixed scenarios,
rubrics, trajectory rendering, and scoring. Use it when you want to run the
same investigation across multiple models and compare their tool usage,
reasoning, and conclusions in a structured, replayable way.

`--record-trajectory` is optional. Add it when you want:
- an **audit trail** of exactly what the agent checked and why
- **screenshots or diagrams** of a run (rendered via `render_trajectory.py`)
- **formal model comparison** with deterministic scoring

### Good alert format

A well-formed alert gives the model a clear symptom, scope, time window, and
output expectation — without leaking the suspected root cause:

> Kafka consumer lag is increasing for multi-system-processor in namespace si
> over the last 2 hours. Investigate using read-only tools. Check logs,
> Kubernetes state, Prometheus, and Event Streams if relevant. Produce an
> incident report with evidence, likely root cause, confidence, safe remediation
> suggestions, and verification steps.

**For fair model evaluation:** do not mention the suspected root cause in the
alert. Give the model the symptom and let it discover the cause from tools and
logs. Keep expected evidence — for example, `UNKNOWN_TOPIC_OR_PARTITION` or
specific topic names — only in `evals/scenarios/*.yaml` (`expected_evidence`),
`evals/rubrics/*.yaml`, and verifier scripts. Do not include them in the alert
string passed to the model.

### Normal one-off investigation

```bash
MODEL_PROVIDER=openai \
MODEL=gpt-5.5 \
python main.py \
  --alert "metric-analyser pods are failing to start in namespace si after the latest deployment. Investigate using read-only tools and produce likely root cause, evidence, and remediation suggestions for human review." \
  --namespace si
```

### One-off investigation with audit trail

Add `--record-trajectory` to capture every step for later review:

```bash
MODEL_PROVIDER=openai \
MODEL=gpt-5.5 \
python main.py \
  --alert "metric-analyser pods are failing to start in namespace si after the latest deployment. Investigate using read-only tools and produce likely root cause, evidence, and remediation suggestions for human review." \
  --namespace si \
  --record-trajectory \
  --scenario-id manual_metric_analyser_startup \
  --skip-preflight
```

This writes:

```
evals/runs/manual_metric_analyser_startup/<provider>/<model>/<run_id>/trajectory.json
evals/runs/manual_metric_analyser_startup/<provider>/<model>/<run_id>/raw/
```

Render the trajectory to Markdown and a Mermaid sequence diagram:

```bash
python evals/render_trajectory.py \
  evals/runs/manual_metric_analyser_startup/.../trajectory.json
# → trajectory.md and trajectory.mmd written next to trajectory.json
```

## Trajectory capture

`--record-trajectory` records every step of an investigation:

- **model messages** — the reasoning text the LLM emitted
- **tool calls** — tool name, arguments, and safety class
- **tool results** — a compact summary in `trajectory.json`; full sanitized
  output in `raw/tool_result_NNN_<tool>.txt`
- **final answer** — the model's concluding report
- **timing and metrics** — duration, tool-call count, failure count
- **safety fields** — flags for mutation attempts, exposed secrets, unsafe
  recommendations

Sensitive values (API keys, tokens, passwords) are redacted before anything is
written to disk. Trajectories are useful both for one-off audit trails and for
structured model comparison.

```bash
MODEL_PROVIDER=openai \
MODEL=gpt-5.5 \
python main.py \
  --alert "Kafka consumer rebalances are spiking in namespace si for multi-system-processor. Investigate the last 2 hours using read-only tools. Produce a concise SRE incident report with evidence, likely root cause, confidence, remediation commands for human review only, and verification steps." \
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

Score a run against the deterministic Kafka rubric:

```bash
python evals/verify_kafka_unknown_topic.py evals/runs/.../trajectory.json
```

Render a run to Markdown + Mermaid sequence diagram:

```bash
python evals/render_trajectory.py evals/runs/.../trajectory.json
# → trajectory.md and trajectory.mmd written next to trajectory.json
```

### Formal model comparison

To compare models fairly, run the **same alert, same namespace, same time
window, and same `--scenario-id`** across all providers. Use a custom
`--runs-dir` to group results under a single directory for easy comparison.

The alert must describe the symptom only — not the suspected cause. Expected
evidence lives in `evals/scenarios/kafka_unknown_topic.yaml` and the rubric,
not in the prompt.

**OpenAI**
```bash
MODEL_PROVIDER=openai \
MODEL=gpt-5.5 \
python main.py \
  --alert "Kafka consumer rebalances are spiking in namespace si for multi-system-processor. Investigate the last 2 hours using read-only tools. Produce a concise SRE incident report with evidence, likely root cause, confidence, remediation commands for human review only, and verification steps." \
  --namespace si \
  --record-trajectory \
  --scenario-id kafka_unknown_topic \
  --runs-dir evals/runs/kafka-model-comparison \
  --skip-preflight
```

**Anthropic**
```bash
MODEL_PROVIDER=anthropic \
MODEL=claude-opus-4-8 \
python main.py \
  --alert "Kafka consumer rebalances are spiking in namespace si for multi-system-processor. Investigate the last 2 hours using read-only tools. Produce a concise SRE incident report with evidence, likely root cause, confidence, remediation commands for human review only, and verification steps." \
  --namespace si \
  --record-trajectory \
  --scenario-id kafka_unknown_topic \
  --runs-dir evals/runs/kafka-model-comparison \
  --skip-preflight
```

**Gemini**
```bash
MODEL_PROVIDER=gemini \
MODEL=gemini-2.5-pro \
python main.py \
  --alert "Kafka consumer rebalances are spiking in namespace si for multi-system-processor. Investigate the last 2 hours using read-only tools. Produce a concise SRE incident report with evidence, likely root cause, confidence, remediation commands for human review only, and verification steps." \
  --namespace si \
  --record-trajectory \
  --scenario-id kafka_unknown_topic \
  --runs-dir evals/runs/kafka-model-comparison \
  --skip-preflight
```

**Ollama / Gemma**
```bash
ollama pull gemma3:12b

MODEL_PROVIDER=ollama \
MODEL=gemma3:12b \
OLLAMA_BASE_URL=http://localhost:11434 \
python main.py \
  --alert "Kafka consumer rebalances are spiking in namespace si for multi-system-processor. Investigate the last 2 hours using read-only tools. Produce a concise SRE incident report with evidence, likely root cause, confidence, remediation commands for human review only, and verification steps." \
  --namespace si \
  --record-trajectory \
  --scenario-id kafka_unknown_topic \
  --runs-dir evals/runs/kafka-model-comparison \
  --skip-preflight
```

**OpenAI-compatible / Nemotron / NVIDIA NIM**
```bash
MODEL_PROVIDER=openai-compatible \
MODEL=<nemotron-or-nim-model-id> \
OPENAI_COMPATIBLE_BASE_URL=<your-endpoint-base-url> \
OPENAI_COMPATIBLE_API_KEY=<your-key-or-dummy> \
python main.py \
  --alert "Kafka consumer rebalances are spiking in namespace si for multi-system-processor. Investigate the last 2 hours using read-only tools. Produce a concise SRE incident report with evidence, likely root cause, confidence, remediation commands for human review only, and verification steps." \
  --namespace si \
  --record-trajectory \
  --scenario-id kafka_unknown_topic \
  --runs-dir evals/runs/kafka-model-comparison \
  --skip-preflight
```

After all runs complete, score and render each trajectory:

```bash
# Score against the deterministic Kafka rubric (100 points, 8 checks)
python evals/verify_kafka_unknown_topic.py \
  evals/runs/kafka-model-comparison/.../trajectory.json

# Render to Markdown + Mermaid sequence diagram
python evals/render_trajectory.py \
  evals/runs/kafka-model-comparison/.../trajectory.json
```

The verifier gives a deterministic score for each model run. The renderer
produces `trajectory.md` (step table + embedded diagram) and `trajectory.mmd`
(Mermaid only), making it easy to compare tool usage and reasoning across
models side by side.

## Files

### Runtime

- `main.py` — entrypoint / CLI
- `agent/loop.py` — ReAct loop and optional trajectory hooks
- `agent/providers.py` — model provider abstraction for OpenAI, Anthropic, Gemini, Ollama, and OpenAI-compatible endpoints
- `agent/prompts.py` — system prompt / SRE instructions
- `tools/registry.py` — tool definitions, safety classification, dispatch, and IBM Event Streams CLI dispatch
- `tools/runner.py` — read-only subprocess runner / command guard
- `tools/prometheus.py` — Prometheus HTTP client
- `tools/ibm_logs.py` — IBM Cloud Logs query client
- `tools/scrubber.py` — output sanitization
- `config.yaml` — per-environment config

### Evaluation

- `evals/trajectory.py` — trajectory recorder for model evaluation and investigation audit trails
- `evals/scenarios/kafka_unknown_topic.yaml` — Kafka rebalance evaluation scenario
- `evals/rubrics/kafka_unknown_topic.yaml` — deterministic scoring rubric
- `evals/verify_kafka_unknown_topic.py` — verifier for Kafka trajectory runs
- `evals/render_trajectory.py` — renders trajectory JSON to Markdown + Mermaid diagram
- `evals/runs/` — generated trajectory outputs, git-ignored

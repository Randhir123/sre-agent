#!/usr/bin/env python3
"""
SRE Agent — CLI entrypoint.

Copy .env.example to .env, fill in values, then run:
    python main.py --alert "Kafka consumer rebalances spiking in namespace si"

Or set env vars manually:
    export MODEL=gpt-4o   # or claude-opus-4-8 (default)
    export ANTHROPIC_API_KEY=sk-ant-...   # if using Claude
    export OPENAI_API_KEY=sk-...          # if using OpenAI
    export IBM_CLOUD_API_KEY=...
    export IBM_LOGS_ENDPOINT=https://<guid>.api.us-south.logs.cloud.ibm.com
"""
from __future__ import annotations

import atexit
import argparse
import os
import sys
import subprocess
import time

# Load .env before anything else so MODEL and API keys are in the environment.
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass  # python-dotenv not installed; fall back to existing env vars

import yaml

from agent.loop import investigate, MODEL, _provider


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        print(f"[warn] config file '{path}' not found, using defaults", file=sys.stderr)
        return {
            "default_namespace": "default",
            "prometheus_url": os.environ.get("PROMETHEUS_URL", "http://localhost:9090"),
        }
    with open(path) as f:
        return yaml.safe_load(f)


def _check(label: str, ok: bool, detail: str = "") -> bool:
    icon = "OK" if ok else "FAIL"
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{icon}] {label}{suffix}")
    return ok


def preflight(cfg: dict) -> bool:
    """
    Verify access to every backend the agent needs before starting.
    Returns True if all required checks pass, False otherwise.
    """
    import requests

    prov = _provider(MODEL)

    print("\nPreflight checks")
    print("─" * 50)
    all_ok = True

    # 0. Active model + provider
    _check(f"model", True, f"{MODEL}  (provider: {prov})")

    # 1. kubectl context
    try:
        result = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True, text=True, timeout=10,
        )
        ctx = result.stdout.strip() or result.stderr.strip()
        ok = result.returncode == 0
    except FileNotFoundError:
        ctx = "kubectl not found"
        ok = False
    except subprocess.TimeoutExpired:
        ctx = "timed out"
        ok = False
    all_ok &= _check("kubectl context", ok, ctx)

    # 2. LLM API key / connectivity
    if prov == "anthropic":
        llm_key_ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
        all_ok &= _check("ANTHROPIC_API_KEY set", llm_key_ok)
    elif prov == "openai":
        llm_key_ok = bool(os.environ.get("OPENAI_API_KEY"))
        all_ok &= _check("OPENAI_API_KEY set", llm_key_ok)
    elif prov == "gemini":
        gem_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        llm_key_ok = bool(gem_key)
        all_ok &= _check("GOOGLE_API_KEY or GEMINI_API_KEY set", llm_key_ok)
    elif prov == "ollama":
        ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        _check("Ollama (no API key required)", True, f"base URL: {ollama_url}")
        try:
            ollama_resp = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=5)
            ollama_ok = ollama_resp.status_code == 200
            ollama_detail = "reachable" if ollama_ok else f"HTTP {ollama_resp.status_code}"
        except Exception as e:
            ollama_ok = False
            ollama_detail = f"unreachable: {e}"
        if not ollama_ok:
            print(f"  [warn] Ollama not reachable at {ollama_url} ({ollama_detail})")
            print("    Use --skip-preflight to bypass, or start Ollama first.")
    elif prov == "openai-compatible":
        compat_url = os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "")
        url_ok = bool(compat_url)
        all_ok &= _check("OPENAI_COMPATIBLE_BASE_URL set", url_ok,
                         compat_url if url_ok else "not set")
        compat_key = os.environ.get("OPENAI_COMPATIBLE_API_KEY", "")
        if not compat_key:
            _check("OPENAI_COMPATIBLE_API_KEY", False,
                   "not set — will use 'dummy'; may fail if endpoint requires auth")
        else:
            _check("OPENAI_COMPATIBLE_API_KEY set", True)
    else:
        _check(f"LLM key for {prov}", False, "unknown provider")

    # 3. IBM Cloud API key
    api_key_set = bool(os.environ.get("IBM_CLOUD_API_KEY"))
    all_ok &= _check("IBM_CLOUD_API_KEY set", api_key_set)

    # 4. IBM Cloud Logs endpoint
    endpoint = os.environ.get("IBM_LOGS_ENDPOINT", "")
    endpoint_set = bool(endpoint)
    all_ok &= _check(
        "IBM_LOGS_ENDPOINT set",
        endpoint_set,
        endpoint.replace(".ingress.", ".api.").rstrip("/") if endpoint_set else "not set",
    )

    # 5. IAM token exchange
    if api_key_set:
        try:
            iam_resp = requests.post(
                "https://iam.cloud.ibm.com/identity/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                    "apikey": os.environ["IBM_CLOUD_API_KEY"],
                },
                timeout=15,
            )
            iam_ok = iam_resp.status_code == 200
            iam_detail = "token obtained" if iam_ok else f"HTTP {iam_resp.status_code}"
        except Exception as e:
            iam_ok = False
            iam_detail = str(e)
        all_ok &= _check("IBM IAM token exchange", iam_ok, iam_detail)
    else:
        _check("IBM IAM token exchange", False, "skipped — IBM_CLOUD_API_KEY not set")
        all_ok = False

    # 6. Prometheus metrics (warning only — agent can still run on logs alone)
    prom_url = cfg.get("prometheus_url", "")
    if prom_url:
        try:
            prom_resp = requests.get(
                f"{prom_url.rstrip('/')}/api/v1/status/config",
                timeout=5,
            )
            prom_ok = prom_resp.status_code == 200
            prom_detail = "reachable" if prom_ok else f"HTTP {prom_resp.status_code}"
        except Exception as e:
            prom_ok = False
            prom_detail = f"unreachable: {e}"
        _check(f"Prometheus ({prom_url})", prom_ok, prom_detail)
        if not prom_ok:
            print("    [warn] Metrics unreachable — will auto-start port-forward before investigating")

    print("─" * 50)
    if not all_ok:
        print("One or more REQUIRED checks failed. Fix them before running the agent.\n")
    return all_ok


def _prom_reachable(url: str) -> bool:
    import requests
    try:
        return requests.get(f"{url.rstrip('/')}/api/v1/status/config", timeout=3).status_code == 200
    except Exception:
        return False


def _ensure_prometheus(cfg: dict) -> bool:
    """
    If Prometheus is already reachable, do nothing and return True.
    Otherwise start a kubectl port-forward as a background process, wait up to
    20 s for it to become ready, register an atexit handler to kill it, and
    return whether it became reachable.
    """
    prom_url = cfg.get("prometheus_url", "http://localhost:9090")

    if _prom_reachable(prom_url):
        return True

    svc  = cfg.get("prometheus_pf_svc",  "kube-prometheus-stack-prometheus")
    ns   = cfg.get("prometheus_pf_ns",   "monitoring")
    port = cfg.get("prometheus_pf_port", 9090)
    target = f"{port}:{port}"

    print(f"  Starting port-forward: kubectl port-forward svc/{svc} {target} -n {ns}")
    try:
        proc = subprocess.Popen(
            ["kubectl", "port-forward", f"svc/{svc}", target, "-n", ns],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("  [FAIL] kubectl not found — cannot start port-forward")
        return False

    atexit.register(proc.terminate)

    for elapsed in range(1, 21):
        time.sleep(1)
        if _prom_reachable(prom_url):
            print(f"  [OK] Prometheus ready after {elapsed}s")
            return True

    print("  [FAIL] Port-forward started but Prometheus not reachable after 20s")
    return False


def main():
    parser = argparse.ArgumentParser(description="Autonomous SRE investigation agent")
    parser.add_argument("--alert", required=True, help="the alert / symptom to investigate")
    parser.add_argument("--namespace", help="override default namespace")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--quiet", action="store_true", help="suppress step-by-step trace")
    parser.add_argument("--skip-preflight", action="store_true", help="skip preflight checks")
    parser.add_argument("--record-trajectory", action="store_true",
                        help="capture investigation trajectory to evals/runs/")
    parser.add_argument("--scenario-id", default="manual",
                        help="scenario label used in trajectory path (default: manual)")
    parser.add_argument("--runs-dir", default="evals/runs",
                        help="root directory for trajectory runs (default: evals/runs)")
    args = parser.parse_args()

    prov = _provider(MODEL)
    _KEY_VARS = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": None,   # checked separately (two possible vars)
        "ollama": None,   # no key required
        "openai-compatible": None,   # key may be "dummy"
    }
    key_var = _KEY_VARS.get(prov)
    if key_var and not os.environ.get(key_var):
        print(f"ERROR: {key_var} not set (model={MODEL}, provider={prov})", file=sys.stderr)
        sys.exit(1)
    if prov == "gemini":
        if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
            print(
                f"ERROR: GOOGLE_API_KEY or GEMINI_API_KEY not set (model={MODEL}, provider={prov})",
                file=sys.stderr,
            )
            sys.exit(1)

    cfg = load_config(args.config)
    if args.namespace:
        cfg["default_namespace"] = args.namespace

    if not args.skip_preflight:
        if not preflight(cfg):
            sys.exit(1)

    _ensure_prometheus(cfg)

    # Optional trajectory recorder — only created when --record-trajectory is set
    recorder = None
    if args.record_trajectory:
        from evals.trajectory import TrajectoryRecorder
        recorder = TrajectoryRecorder(
            scenario_id=args.scenario_id,
            model=MODEL,
            provider=prov,
            alert=args.alert,
            config={
                "namespace": cfg.get("default_namespace"),
                "prometheus_url": cfg.get("prometheus_url"),
                "config_path": args.config,
                "quiet": args.quiet,
                "skip_preflight": args.skip_preflight,
            },
            out_dir=args.runs_dir,
        )

    print(f"\nInvestigating: {args.alert}")
    print(f"   namespace : {cfg.get('default_namespace')}")
    print(f"   model     : {MODEL}  ({prov})")

    report = investigate(args.alert, cfg, verbose=not args.quiet, recorder=recorder)

    print("\n" + "=" * 70)
    print("FINAL REPORT")
    print("=" * 70)
    print(report)

    if recorder and recorder.saved_path:
        print(f"\nTrajectory saved: {recorder.saved_path}")


if __name__ == "__main__":
    main()

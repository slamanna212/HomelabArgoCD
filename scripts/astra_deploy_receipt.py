#!/usr/bin/env python3
"""Read-only Astra deployment receipt. Needs gh+GH_TOKEN and in-cluster DNS.

Run after a HomelabArgoCD image-pin change. This tool never deploys, restarts,
logs in, or creates a test session. Authentication is explicitly UNVERIFIED.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "slamanna212/HomelabArgoCD"
ASTRA = "slamanna212/Astra"
MCP = "http://homelab-mcp.homelab-mcp.svc.cluster.local:8000/mcp"
PROM = "http://prometheus-operated.monitoring.svc.cluster.local:9090"
HEALTH = "http://hermes.hermes.svc.cluster.local:9090/api/health"
MANIFEST = "workloads/hermes/deployment.yaml"
IMAGE_RE = re.compile(r"(?m)^\s*image:\s*(ghcr\.io/slamanna212/astra:sha-([0-9a-f]{40}))\s*$")


def image_sha(image: str) -> str | None:
    match = re.fullmatch(r"ghcr\.io/slamanna212/astra:sha-([0-9a-f]{40})", image)
    return match.group(1) if match else None


def evaluate(data: dict) -> dict:
    """Evaluate exact observed evidence; unknown and mismatched are never green."""
    checks = []

    def check(name: str, ok: bool, evidence: object) -> None:
        checks.append({"name": name, "state": "pass" if ok else "fail", "evidence": evidence})

    sha = image_sha(data.get("manifest_image", ""))
    check("gitops_image_pin", bool(sha), data.get("manifest_image"))
    commit = data.get("commit") or {}
    check("source_commit", bool(sha and commit.get("sha") == sha), commit.get("sha"))
    runs = data.get("build_runs") or []
    published = [run for run in runs if run.get("head_sha") == sha and run.get("event") in ("push", "workflow_dispatch") and run.get("status") == "completed" and run.get("conclusion") == "success"]
    check("image_build", bool(published), [run.get("html_url") for run in published])
    argo = data.get("argo") or {}
    head = data.get("gitops_head") or ""
    check("argocd_revision", len(argo.get("revision") or "") >= 7 and head.startswith(argo.get("revision") or "not-a-revision"), {"source_head": head, "observed_revision": argo.get("revision")})
    check("argocd_state", argo.get("sync") == "Synced" and argo.get("health") == "Healthy", {"sync": argo.get("sync"), "health": argo.get("health")})
    workload = data.get("workload") or {}
    check("rollout", workload.get("desired") == 1 and workload.get("ready") == 1 and workload.get("available") == 1 and workload.get("restarts") == 0, {key: workload.get(key) for key in ("desired", "ready", "available", "restarts")})
    container = data.get("container") or {}
    check("runtime_image", container.get("image") == data.get("manifest_image") and bool(re.fullmatch(r"ghcr\.io/slamanna212/astra@sha256:[0-9a-f]{64}", container.get("image_id") or "")), {key: container.get(key) for key in ("image", "image_id", "pod")})
    health = data.get("health") or {}
    check("health", health.get("status") == "ok" and (health.get("state_db") or {}).get("ok") is True, health)
    checks.append({"name": "authenticated_user_flow", "state": "unverified", "evidence": "No credential or production test session used; /api/health is not a chat test."})
    verdict = "fail" if any(c["state"] == "fail" for c in checks) else "partial"
    return {"verdict": verdict, "source_sha": sha, "checks": checks}


def github(path: str, raw: bool = False) -> object:
    cmd = ["gh", "api"]
    if raw:
        cmd += ["-H", "Accept: application/vnd.github.raw+json"]
    cmd.append("/repos/" + path)
    result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    return result.stdout if raw else json.loads(result.stdout)


def http_json(url: str, *, timeout: int = 15) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def mcp_post(payload: dict, session: str | None = None) -> tuple[dict, str | None]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(MCP, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        text, sid = response.read().decode(), response.headers.get("Mcp-Session-Id")
    messages = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    return (messages[-1] if messages else json.loads(text) if text else {}), sid


def mcp_tool(name: str, args: dict) -> dict:
    _, sid = mcp_post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "astra-receipt", "version": "1"}}})
    mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    reply, _ = mcp_post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}, sid)
    result = reply.get("result") or {}
    if reply.get("error") or result.get("isError") or not result.get("content"):
        raise RuntimeError(f"{name}: MCP returned an error or empty result")
    return json.loads(result["content"][0]["text"])


def collect() -> dict:
    manifest = github(f"{REPO}/contents/{MANIFEST}", raw=True)
    matches = IMAGE_RE.findall(manifest)
    if len(matches) != 1:
        raise ValueError(f"Expected one exact Astra SHA image in {MANIFEST}; got {len(matches)}")
    image, sha = matches[0]
    gitops_head = github(f"{REPO}/commits/main")["sha"]
    commit = github(f"{ASTRA}/commits/{sha}")
    build_runs = github(f"{ASTRA}/actions/workflows/container.yml/runs?head_sha={sha}&per_page=30")["workflow_runs"]
    argo = next((a for a in mcp_tool("get_argocd_apps", {})["apps"] if a["name"] == "hermes"), None)
    workload = mcp_tool("get_workload_status", {"namespace": "hermes", "name": "hermes"})
    query = urllib.parse.urlencode({"query": 'kube_pod_container_info{namespace="hermes",container="astra"}'})
    samples = http_json(f"{PROM}/api/v1/query?{query}")["data"]["result"]
    # A stale/duplicated container sample is ambiguous; refuse to select one.
    container = None
    if len(samples) == 1 and samples[0].get("value", [None, "0"])[1] == "1":
        metric = samples[0]["metric"]
        container = {key: metric.get(key) for key in ("image", "image_id", "pod")}
    return {"manifest_image": image, "gitops_head": gitops_head, "commit": {"sha": commit["sha"]}, "build_runs": build_runs, "argo": argo, "workload": workload, "container": container, "health": http_json(HEALTH)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the receipt as JSON (without credentials).")
    args = parser.parse_args()
    try:
        result = evaluate(collect())
    except (OSError, ValueError, KeyError, IndexError, RuntimeError, subprocess.SubprocessError) as exc:
        result = {"verdict": "fail", "source_sha": None, "checks": [{"name": "evidence_collection", "state": "fail", "evidence": f"{type(exc).__name__}: {exc}"}]}
    result["checked_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    document = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(document)
    print(document, end="")
    return 2 if result["verdict"] == "fail" else 3 if result["verdict"] == "partial" else 0


if __name__ == "__main__":
    sys.exit(main())

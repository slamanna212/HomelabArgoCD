#!/usr/bin/env python3
"""Prometheus exporter for AI allowance/usage across providers.

Runs `codexbar dashboard` (schema-v1 snapshot) on an interval and exposes
the parsed metrics on :9100/metrics.
https://github.com/steipete/CodexBar/blob/main/docs/dashboard-api.md
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

from prometheus_client import Gauge, start_http_server

CODEXBAR_BIN = os.environ.get("CODEXBAR_BIN", "/codexbar/codexbar")
CODEXBAR_CONFIG = os.environ.get("CODEXBAR_CONFIG", "/data/codexbar/config.json")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "300"))
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "120"))
PORT = int(os.environ.get("PORT", "9100"))

gauge_provider_up = Gauge("ai_provider_up", "Whether the provider's usage fetch succeeded", ["provider"])
gauge_provider_status = Gauge(
    "ai_provider_status", "Provider status level: 0=ok, 1=warning, 2=critical, 3=unknown", ["provider"]
)
gauge_provider_updated = Gauge("ai_provider_updated_timestamp", "Last successful update (unix)", ["provider"])
gauge_provider_error = Gauge("ai_provider_error", "1 if the provider's latest fetch failed", ["provider"])

gauge_used_percent = Gauge("ai_usage_used_percent", "Usage used percent for window", ["provider", "window"])
gauge_remaining_percent = Gauge("ai_usage_remaining_percent", "Usage remaining percent for window", ["provider", "window"])
gauge_reset_ts = Gauge("ai_usage_reset_timestamp", "Window reset time (unix)", ["provider", "window"])
gauge_reset_in = Gauge("ai_usage_reset_seconds", "Seconds until window reset", ["provider", "window"])

gauge_credits_remaining = Gauge("ai_credits_remaining", "Remaining credits/balance", ["provider"])
gauge_credits_unit = Gauge("ai_credits_unit", "Unit of the credit value (1=credits, 2=USD, 3=other)", ["provider"])

gauge_cost_today = Gauge("ai_cost_today_usd", "Estimated cost today (USD)", ["provider"])
gauge_cost_30d = Gauge("ai_cost_last30d_usd", "Estimated cost last 30 days (USD)", ["provider"])

gauge_collector_up = Gauge("ai_collector_up", "1 if collector itself is healthy", [])

STATUS_LEVELS = {"ok": 0, "warning": 1, "critical": 2, "unknown": 3}


def _to_ts(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _run_dashboard():
    # Keep the codex credential mirrored on every cycle.
    try:
        subprocess.run([os.environ.get("PYTHON", "python3"), "/app/sync_codex.py"],
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass
    cmd = [CODEXBAR_BIN, "dashboard", "--identity", "redacted", "--timeout", str(FETCH_TIMEOUT)]
    try:
        env = dict(os.environ)
        env["CODEXBAR_CONFIG"] = CODEXBAR_CONFIG
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FETCH_TIMEOUT + 30, env=env)
        if proc.returncode != 0:
            print(f"codexbar dashboard exited {proc.returncode}: {proc.stderr[:500]}", flush=True)
            return None
        return json.loads(proc.stdout)
    except Exception as exc:
        print(f"codexbar dashboard failed: {exc}", flush=True)
        return None


def _refresh():
    now = time.time()
    snap = _run_dashboard()
    if snap is None:
        gauge_collector_up.set(0)
        return
    gauge_collector_up.set(1)

    for prov in snap.get("providers", []):
        pid = prov.get("id")
        if not pid:
            continue
        error = prov.get("error")
        gauge_provider_up.labels(pid).set(1 if error is None else 0)
        gauge_provider_error.labels(pid).set(1 if error is not None else 0)
        status = (prov.get("status") or {}).get("level", "unknown")
        gauge_provider_status.labels(pid).set(STATUS_LEVELS.get(status, 3))
        updated = _to_ts(prov.get("updatedAt"))
        gauge_provider_updated.labels(pid).set(updated if updated else now)

        for win in prov.get("windows") or []:
            kind = win.get("kind") or "window"
            used = win.get("usedPercent")
            remaining = win.get("remainingPercent")
            reset_at = _to_ts(win.get("resetAt"))
            if used is not None:
                gauge_used_percent.labels(pid, kind).set(used)
            if remaining is not None:
                gauge_remaining_percent.labels(pid, kind).set(remaining)
            if reset_at:
                gauge_reset_ts.labels(pid, kind).set(reset_at)
                gauge_reset_in.labels(pid, kind).set(max(0, reset_at - now))

        credits = prov.get("credits")
        if credits and credits.get("remaining") is not None:
            gauge_credits_remaining.labels(pid).set(credits["remaining"])
            unit = (credits.get("unit") or "").lower()
            if unit in ("usd", "$"):
                gauge_credits_unit.labels(pid).set(2)
            elif unit in ("credits", "credit"):
                gauge_credits_unit.labels(pid).set(1)
            else:
                gauge_credits_unit.labels(pid).set(3)

        cost = prov.get("cost") or {}
        if cost.get("todayUSD") is not None:
            gauge_cost_today.labels(pid).set(cost["todayUSD"])
        if cost.get("last30DaysUSD") is not None:
            gauge_cost_30d.labels(pid).set(cost["last30DaysUSD"])


def _loop():
    while True:
        try:
            _refresh()
        except Exception as exc:
            print(f"refresh error: {exc}", flush=True)
            gauge_collector_up.set(0)
        time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=_loop, daemon=True).start()
    start_http_server(PORT)
    print(f"ai-usage-exporter listening on :{PORT}", flush=True)
    while True:
        time.sleep(3600)

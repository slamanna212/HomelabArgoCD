#!/usr/bin/env python3
"""Standalone Prometheus collector for Claude, Codex, OpenRouter, and DeepSeek.

Claude subscription usage follows the OAuth PKCE/token-refresh logic used by
trickv/hass-claude-usage, without requiring Home Assistant or the Claude CLI.
Codex reads the Hermes gateway's live OAuth credential read-only; the gateway
owns refresh-token rotation.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
CLAUDE_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLAUDE_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_BETA_HEADER = "oauth-2025-04-20"
CLAUDE_DEFAULT_SCOPES = "user:profile"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"

STATUS_LEVELS = {"ok": 0, "warning": 1, "critical": 2, "unknown": 3}


class ProviderError(RuntimeError):
    """An error safe to report without including credentials or response bodies."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _timestamp(value: Any) -> float | None:
    if value is None:
        return None
    numeric = _number(value)
    if numeric is not None:
        return numeric
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    chars = [c if c.isalnum() else "_" for c in text]
    result = "".join(chars).strip("_")
    while "__" in result:
        result = result.replace("__", "_")
    return result[:100] or "unknown"


def _request_json(
    url: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    body = None
    request_headers = {"Accept": "application/json", "User-Agent": "ai-usage-collector/1"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=request_headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        # Never include the response body: OAuth and provider error responses can
        # contain account-specific data or accidentally echo request material.
        raise ProviderError(f"HTTP {error.code}", error.code) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ProviderError(f"network error: {type(error).__name__}") from error
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError("invalid JSON response") from error
    if not isinstance(decoded, dict):
        raise ProviderError("JSON response was not an object")
    return decoded


def _atomic_write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _read_json(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise ProviderError(f"could not read state file: {type(error).__name__}") from error
    if not isinstance(value, dict):
        raise ProviderError("state file was not an object")
    return value


def _claude_state_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CLAUDE_STATE", "/data/claude/oauth.json"))


def _claude_pending_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CLAUDE_PENDING_STATE", "/data/claude/oauth-pending.json"))


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def create_claude_auth_url() -> str:
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    scopes = os.environ.get("CLAUDE_OAUTH_SCOPES", CLAUDE_DEFAULT_SCOPES)
    _atomic_write_json(
        _claude_pending_path(),
        {"verifier": verifier, "state": state, "scopes": scopes, "expires_at": time.time() + 900},
    )
    params = {
        "code": "true",
        "client_id": CLAUDE_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CLAUDE_REDIRECT_URI,
        "scope": scopes,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{CLAUDE_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_claude_code(code_input: str) -> dict[str, Any]:
    pending_path = _claude_pending_path()
    pending = _read_json(pending_path)
    if not pending or not pending.get("verifier") or not pending.get("state"):
        raise ProviderError("no pending Claude OAuth flow; run auth-url first")
    if _number(pending.get("expires_at")) and time.time() > float(pending["expires_at"]):
        raise ProviderError("pending Claude OAuth flow expired; run auth-url again")
    parts = code_input.strip().split("#", 1)
    code = parts[0]
    returned_state = parts[1] if len(parts) == 2 else str(pending["state"])
    if not code:
        raise ProviderError("authorization code was empty")
    if returned_state != pending["state"]:
        raise ProviderError("OAuth state mismatch")
    token_data = _request_json(
        CLAUDE_TOKEN_URL,
        payload={
            "grant_type": "authorization_code",
            "code": code,
            "state": returned_state,
            "client_id": CLAUDE_CLIENT_ID,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "code_verifier": pending["verifier"],
        },
        timeout=30,
    )
    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ProviderError("token exchange response did not contain access_token")
    refresh_token = token_data.get("refresh_token")
    if not isinstance(refresh_token, str):
        refresh_token = ""
    state = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + (_number(token_data.get("expires_in")) or 3600),
        "scopes": pending.get("scopes", CLAUDE_DEFAULT_SCOPES),
        "updated_at": time.time(),
    }
    _atomic_write_json(_claude_state_path(), state)
    try:
        profile = _request_json(
            CLAUDE_PROFILE_URL,
            headers={"Authorization": f"Bearer {access_token}", "anthropic-beta": CLAUDE_BETA_HEADER},
            timeout=15,
        )
    except ProviderError:
        profile = {}
    pending_path.unlink(missing_ok=True)
    account = profile.get("account") if isinstance(profile.get("account"), dict) else {}
    return {"saved": True, "email": account.get("email"), "account_id": account.get("uuid") or account.get("id")}


def _refresh_claude_token(state: dict[str, Any], force: bool = False) -> dict[str, Any]:
    expires_at = _number(state.get("expires_at")) or 0
    if not force and time.time() < expires_at - 60:
        return state
    refresh_token = state.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ProviderError("Claude OAuth refresh token is missing")
    token_data = _request_json(
        CLAUDE_TOKEN_URL,
        payload={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLAUDE_CLIENT_ID,
        },
        timeout=30,
    )
    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ProviderError("Claude OAuth refresh response did not contain access_token")
    new_state = {
        **state,
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token") or refresh_token,
        "expires_at": time.time() + (_number(token_data.get("expires_in")) or 3600),
        "updated_at": time.time(),
    }
    _atomic_write_json(_claude_state_path(), new_state)
    return new_state


def _claude_window(name: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used = _number(value.get("utilization"))
    if used is None:
        return None
    result: dict[str, Any] = {"window": name, "used_percent": used, "reset_at": _timestamp(value.get("resets_at"))}
    result["remaining_percent"] = max(0.0, 100.0 - used)
    return result


def _spend_amount(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    amount = _number(value.get("amount_minor"))
    exponent = _number(value.get("exponent"))
    if amount is None:
        return None
    return amount / (10 ** int(exponent if exponent is not None else 2))


def parse_claude_usage(raw: dict[str, Any]) -> dict[str, Any]:
    windows: list[dict[str, Any]] = []
    for name, key in (
        ("session", "five_hour"),
        ("weekly", "seven_day"),
        ("weekly_sonnet", "seven_day_sonnet"),
        ("weekly_opus", "seven_day_opus"),
        ("weekly_oauth_apps", "seven_day_oauth_apps"),
        ("weekly_routines", "seven_day_routines"),
        ("iguana_necktie", "iguana_necktie"),
    ):
        window = _claude_window(name, raw.get(key))
        if window:
            windows.append(window)
    for entry in raw.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        used = _number(entry.get("percent"))
        if used is None:
            continue
        scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
        model_data = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        model = model_data.get("display_name")
        surface = scope.get("surface")
        parts = [_slug(entry.get("kind") or "limit")]
        if model:
            parts.append(_slug(model))
        if surface:
            parts.append(_slug(surface))
        windows.append(
            {
                "window": "claude_" + "_".join(parts),
                "used_percent": used,
                "remaining_percent": max(0.0, 100.0 - used),
                "reset_at": _timestamp(entry.get("resets_at")),
                "active": entry.get("is_active"),
                "severity": entry.get("severity"),
            }
        )
    extra: dict[str, Any] | None = None
    old_extra = raw.get("extra_usage")
    if isinstance(old_extra, dict):
        enabled = bool(old_extra.get("is_enabled"))
        divisor = 10 ** int(_number(old_extra.get("decimal_places")) or 2)
        used = _number(old_extra.get("used_credits"))
        limit = _number(old_extra.get("monthly_limit"))
        extra = {
            "enabled": enabled,
            "used": used / divisor if used is not None else None,
            "limit": limit / divisor if limit is not None else None,
            "percent": _number(old_extra.get("utilization")),
            "unit": "usd",
        }
    spend = raw.get("spend")
    if isinstance(spend, dict) and spend.get("enabled"):
        extra = {
            "enabled": True,
            "used": _spend_amount(spend.get("used")),
            "limit": _spend_amount(spend.get("limit")),
            "percent": _number(spend.get("percent")),
            "unit": str((spend.get("used") or {}).get("currency") or "usd").lower(),
        }
    return {"windows": windows, "extra": extra}


def _codex_window(name: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used = _number(value.get("used_percent"))
    reset_at = _timestamp(value.get("reset_at"))
    if used is None and reset_at is None:
        return None
    result = {"window": name, "used_percent": used or 0.0, "reset_at": reset_at}
    result["remaining_percent"] = max(0.0, 100.0 - result["used_percent"])
    return result


def parse_codex_usage(raw: dict[str, Any]) -> dict[str, Any]:
    rate_limit = raw.get("rate_limit") if isinstance(raw.get("rate_limit"), dict) else {}
    windows: list[dict[str, Any]] = []
    for name, key in (("session", "primary_window"), ("weekly", "secondary_window")):
        window = _codex_window(name, rate_limit.get(key))
        if window:
            windows.append(window)
    for extra in raw.get("additional_rate_limits") or []:
        if not isinstance(extra, dict):
            continue
        label = _slug(extra.get("limit_name") or extra.get("metered_feature") or "additional")
        nested = extra.get("rate_limit") if isinstance(extra.get("rate_limit"), dict) else {}
        for name, key in ((f"{label}_session", "primary_window"), (f"{label}_weekly", "secondary_window")):
            window = _codex_window(name, nested.get(key))
            if window:
                windows.append(window)
    credits = raw.get("credits") if isinstance(raw.get("credits"), dict) else {}
    balance = _number(credits.get("balance"))
    result: dict[str, Any] = {"windows": windows, "plan": raw.get("plan_type")}
    if balance is not None:
        result["credits"] = {"remaining": balance, "unit": "credits"}
    return result


def parse_openrouter_credits(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    total = _number(data.get("total_credits"))
    used = _number(data.get("total_usage"))
    remaining = _number(data.get("remaining"))
    if remaining is None and total is not None and used is not None:
        remaining = total - used
    if remaining is None:
        raise ProviderError("OpenRouter response did not contain a balance")
    return {"windows": [], "credits": {"remaining": remaining, "unit": "usd"}}


def parse_deepseek_balance(raw: dict[str, Any]) -> dict[str, Any]:
    infos = raw.get("balance_infos")
    if isinstance(infos, list):
        candidates = [item for item in infos if isinstance(item, dict) and str(item.get("currency", "")).upper() == "USD"]
        item = (candidates or [item for item in infos if isinstance(item, dict)] or [None])[0]
        if isinstance(item, dict):
            balance = _number(item.get("total_balance"))
            if balance is not None:
                return {"windows": [], "credits": {"remaining": balance, "unit": "usd"}}
    for key in ("balance", "total_balance"):
        balance = _number(raw.get(key))
        if balance is not None:
            return {"windows": [], "credits": {"remaining": balance, "unit": "usd"}}
    raise ProviderError("DeepSeek response did not contain a USD balance")


class Metrics:
    def __init__(self) -> None:
        from prometheus_client import Gauge

        self.provider_up = Gauge("ai_provider_up", "Whether the provider usage fetch succeeded", ["provider"])
        self.provider_status = Gauge("ai_provider_status", "Provider status: 0=ok, 1=warning, 2=critical, 3=unknown", ["provider"])
        self.provider_updated = Gauge("ai_provider_updated_timestamp", "Last successful provider update", ["provider"])
        self.provider_error = Gauge("ai_provider_error", "1 when the provider fetch is failing", ["provider"])
        self.provider_plan = Gauge("ai_provider_plan_info", "Current provider plan, value is always 1", ["provider", "plan"])
        self.used = Gauge("ai_usage_used_percent", "Usage used percent for a window", ["provider", "window"])
        self.remaining = Gauge("ai_usage_remaining_percent", "Usage remaining percent for a window", ["provider", "window"])
        self.reset_at = Gauge("ai_usage_reset_timestamp", "Window reset time as Unix seconds", ["provider", "window"])
        self.reset_in = Gauge("ai_usage_reset_seconds", "Seconds until the window resets", ["provider", "window"])
        self.active = Gauge("ai_usage_bucket_active", "1 when a usage bucket is present in the latest response", ["provider", "window"])
        self.credits = Gauge("ai_credits_remaining", "Remaining prepaid credits or balance", ["provider"])
        self.credit_unit = Gauge("ai_credits_unit", "Credit unit: 1=credits, 2=USD, 3=other", ["provider"])
        self.extra_enabled = Gauge("ai_extra_usage_enabled", "1 when Claude extra usage is enabled", ["provider"])
        self.extra_percent = Gauge("ai_extra_usage_used_percent", "Claude extra usage used percent", ["provider"])
        self.extra_used = Gauge("ai_extra_usage_used", "Claude extra usage consumed", ["provider"])
        self.extra_limit = Gauge("ai_extra_usage_limit", "Claude extra usage limit", ["provider"])
        self.collector_up = Gauge("ai_collector_up", "1 when the collector process is healthy", [])
        self._windows: dict[str, set[str]] = {}

    def mark_error(self, provider: str) -> None:
        self.provider_up.labels(provider).set(0)
        self.provider_error.labels(provider).set(1)
        self.provider_status.labels(provider).set(STATUS_LEVELS["unknown"])

    def publish(self, provider: str, result: dict[str, Any], now: float) -> None:
        self.provider_up.labels(provider).set(1)
        self.provider_error.labels(provider).set(0)
        windows = result.get("windows") or []
        current = {str(item.get("window")) for item in windows if item.get("window")}
        known = self._windows.setdefault(provider, set())
        known.update(current)
        for window in known:
            self.active.labels(provider, window).set(1 if window in current else 0)
        highest = 0.0
        for item in windows:
            window = str(item.get("window"))
            used = _number(item.get("used_percent"))
            if used is None:
                continue
            highest = max(highest, used)
            self.used.labels(provider, window).set(used)
            self.remaining.labels(provider, window).set(max(0.0, 100.0 - used))
            reset_at = _number(item.get("reset_at"))
            if reset_at is not None:
                self.reset_at.labels(provider, window).set(reset_at)
                self.reset_in.labels(provider, window).set(max(0.0, reset_at - now))
        status = "critical" if highest >= 95 else "warning" if highest >= 80 else "ok"
        self.provider_status.labels(provider).set(STATUS_LEVELS[status])
        self.provider_updated.labels(provider).set(now)
        plan = result.get("plan")
        if plan:
            self.provider_plan.labels(provider, _slug(plan)).set(1)
        credits = result.get("credits")
        if isinstance(credits, dict) and _number(credits.get("remaining")) is not None:
            self.credits.labels(provider).set(float(credits["remaining"]))
            unit = str(credits.get("unit") or "other").lower()
            self.credit_unit.labels(provider).set(2 if unit in ("usd", "$") else 1 if unit in ("credit", "credits") else 3)
        extra = result.get("extra")
        if isinstance(extra, dict):
            self.extra_enabled.labels(provider).set(1 if extra.get("enabled") else 0)
            for metric, key in ((self.extra_percent, "percent"), (self.extra_used, "used"), (self.extra_limit, "limit")):
                value = _number(extra.get(key))
                if value is not None:
                    metric.labels(provider).set(value)


def _gateway_codex_credentials() -> tuple[str, str | None]:
    path = pathlib.Path(os.environ.get("GATEWAY_AUTH", "/creds/home/auth.json"))
    store = _read_json(path)
    if not store:
        raise ProviderError("Hermes gateway auth.json is missing")
    pool = store.get("credential_pool") if isinstance(store.get("credential_pool"), dict) else {}
    credentials = pool.get("openai-codex") or []
    if isinstance(credentials, dict):
        credentials = [credentials]
    if not credentials or not isinstance(credentials[0], dict):
        raise ProviderError("Hermes gateway has no openai-codex OAuth credential")
    credential = credentials[0]
    token = credential.get("access_token")
    if not isinstance(token, str) or not token:
        raise ProviderError("Hermes openai-codex credential has no access token")
    account_id = credential.get("id") or credential.get("account_id")
    return token, str(account_id) if account_id else None


def fetch_claude() -> dict[str, Any]:
    state = _read_json(_claude_state_path())
    if not state or not state.get("access_token"):
        raise ProviderError("Claude OAuth is not configured; run auth-url and auth-exchange")
    state = _refresh_claude_token(state)
    headers = {
        "Authorization": f"Bearer {state['access_token']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": CLAUDE_BETA_HEADER,
        "User-Agent": "claude-code/2.1.0",
    }
    try:
        raw = _request_json(CLAUDE_USAGE_URL, headers=headers)
    except ProviderError as error:
        if error.status != 401:
            raise
        state = _refresh_claude_token(state, force=True)
        headers["Authorization"] = f"Bearer {state['access_token']}"
        raw = _request_json(CLAUDE_USAGE_URL, headers=headers)
    return parse_claude_usage(raw)


def fetch_codex() -> dict[str, Any]:
    token, account_id = _gateway_codex_credentials()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "ai-usage-collector/1"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return parse_codex_usage(_request_json(CODEX_USAGE_URL, headers=headers))


def fetch_openrouter() -> dict[str, Any]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ProviderError("OpenRouter API key is not configured")
    return parse_openrouter_credits(_request_json(OPENROUTER_CREDITS_URL, headers={"Authorization": f"Bearer {key}"}))


def fetch_deepseek() -> dict[str, Any]:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise ProviderError("DeepSeek API key is not configured")
    return parse_deepseek_balance(_request_json(DEEPSEEK_BALANCE_URL, headers={"Authorization": f"Bearer {key}"}))


class Collector:
    def __init__(self, metrics: Metrics) -> None:
        self.metrics = metrics

    def refresh(self) -> None:
        now = time.time()
        for provider, fetch in (
            ("claude", fetch_claude),
            ("codex", fetch_codex),
            ("openrouter", fetch_openrouter),
            ("deepseek", fetch_deepseek),
        ):
            try:
                self.metrics.publish(provider, fetch(), now)
                print(f"{provider}: ok", flush=True)
            except ProviderError as error:
                self.metrics.mark_error(provider)
                print(f"{provider}: {error}", flush=True)
            except Exception as error:  # Keep one provider failure from stopping the others.
                self.metrics.mark_error(provider)
                print(f"{provider}: unexpected {type(error).__name__}", flush=True)


def run_server() -> None:
    from prometheus_client import start_http_server

    port = int(os.environ.get("PORT", "9100"))
    interval = max(60, int(os.environ.get("REFRESH_INTERVAL", "300")))
    metrics = Metrics()
    metrics.collector_up.set(1)
    start_http_server(port)
    collector = Collector(metrics)
    print(f"ai-usage-collector listening on :{port}", flush=True)
    while True:
        collector.refresh()
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("run", "auth-url", "auth-exchange"), default="run")
    parser.add_argument("--code", help="Claude authorization code; omit to read it securely from stdin")
    args = parser.parse_args()
    if args.command == "auth-url":
        print(create_claude_auth_url())
        return
    if args.command == "auth-exchange":
        code = args.code or input("Paste the Claude authorization code (code#state): ").strip()
        result = exchange_claude_code(code)
        identity = result.get("email") or result.get("account_id") or "account"
        print(f"Claude OAuth credentials saved for {identity}")
        return
    run_server()


if __name__ == "__main__":
    main()

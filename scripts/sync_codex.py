#!/usr/bin/env python3
"""Mirror the gateway's live openai-codex OAuth credential into codexbar's
expected ~/.codex/auth.json shape. Runs before every exporter refresh."""
import json, os, pathlib, time

AUTH_STORE = pathlib.Path(os.environ.get("GATEWAY_AUTH", "/creds/home/auth.json"))
CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", "/data/codex"))
OUT = CODEX_HOME / "auth.json"

def sync():
    if not AUTH_STORE.exists():
        return False
    store = json.loads(AUTH_STORE.read_text())
    pool = store.get("credential_pool", {})
    creds = pool.get("openai-codex") or []
    if not creds:
        return False
    c = creds[0]
    tokens = {
        "access_token": c.get("access_token", ""),
        "refresh_token": c.get("refresh_token", ""),
        "account_id": c.get("id", ""),
    }
    if not tokens["access_token"]:
        return False
    payload = {
        "tokens": tokens,
        "last_refresh": c.get("last_refresh") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "OPENAI_API_KEY": "",
    }
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload))
    os.chmod(OUT, 0o600)
    return True

if __name__ == "__main__":
    ok = sync()
    print(f"codex sync: {'ok' if ok else 'NO CREDENTIAL (gateway auth.json missing openai-codex)'}", flush=True)

# AI usage monitoring

The AI usage collector runs as an unprivileged sidecar in the Hermes pod. It polls
Claude subscription usage, Codex subscription usage, OpenRouter credits, and DeepSeek
balance every five minutes and exposes them on `:9100/metrics` for Prometheus.

## One-time Claude authorization

Claude's consumer subscription usage endpoint requires an OAuth session. The collector
stores the access/refresh tokens in the `ai-usage` subdirectory of the existing Hermes
PVC with mode `0600`; the gateway and collector do not share a writable credential file.

After ArgoCD deploys the sidecar:

```bash
kubectl -n hermes exec -it deploy/hermes -c ai-usage-collector -- python3 /app/collector.py auth-url
```

Open the printed URL in a browser, sign in to Claude, and copy the authorization code
from the callback page. Then exchange it in the sidecar:

```bash
kubectl -n hermes exec -it deploy/hermes -c ai-usage-collector -- python3 /app/collector.py auth-exchange
```

Paste the callback value when prompted. The collector accepts the addon's
`code#state` format, validates the stored PKCE state, atomically writes the token state,
and refreshes access tokens automatically before expiry. No Home Assistant or Claude
CLI is installed.

If Anthropic eventually revokes the refresh session, repeat those two commands. That is
an OAuth-provider reauthorization event, not a scheduled supervision requirement.

## Data sources

| Provider | Source | Authentication |
| --- | --- | --- |
| Claude subscription | `https://api.anthropic.com/api/oauth/usage` | Persisted OAuth PKCE + refresh token |
| Codex subscription | `https://chatgpt.com/backend-api/wham/usage` | Read-only mirror of the Hermes gateway's live OAuth |
| OpenRouter | `/api/v1/credits` | Existing `hermes-secrets` API key |
| DeepSeek | `/user/balance` | Existing `hermes-secrets` API key |

The Codex credential is deliberately read-only in the collector. The Hermes gateway owns
refresh-token rotation because refreshing the same Codex credential from two processes
could strand a rotated token in the wrong file.

## Prometheus and Grafana

The `ai-usage-exporter` Service is scraped by the cluster-wide kube-prometheus-stack
ServiceMonitor. The dashboard ConfigMap is labeled for Grafana's dashboard sidecar and
is titled **AI Allowance & Usage**.

Important metrics include:

- `ai_usage_used_percent` and `ai_usage_reset_seconds` for Claude, Codex, and dynamic
  provider limit buckets
- `ai_extra_usage_*` for Claude overage/spend data
- `ai_credits_remaining` for OpenRouter, DeepSeek, and Codex credits
- `ai_provider_up`, `ai_provider_error`, and `ai_provider_updated_timestamp`
- `ai_collector_up` for the collector process itself

# degoog

Self-hosted search aggregator (Degoog + Valkey), the replacement for the Unraid
SearXNG container at `10.1.10.85:8080`.

- UI: `http://10.1.20.207:4444/` (MetalLB, pinned on the k8s VLAN)
- In-cluster: `http://degoog.degoog.svc.cluster.local:4444/`
- Valkey: `degoog-valkey:6379`, ClusterIP only, cache + invalidation bus, no persistence

## How the two existing consumers reach it (the `/api` seam)

Neither consumer needed a code change, and no path-rewriting proxy was added.
Both build their request as `<base>/search`, so the base URL is pointed at
`http://degoog.degoog.svc.cluster.local:4444/api`:

| Consumer | Where | Value |
| --- | --- | --- |
| Hermes `web_search` (`web.search_backend: searxng`) | live `$HERMES_HOME/.env` (not in this repo) | `SEARXNG_URL=http://degoog.degoog.svc.cluster.local:4444/api` |
| Firecrawl `/v2/search` | `values/firecrawl.yaml` | `SEARXNG_ENDPOINT=http://degoog.degoog.svc.cluster.local:4444/api` |

Verified against source before relying on it: `plugins/web/searxng/provider.py`
does `f"{base_url}/search"` and Firecrawl's `apps/api/src/search/v2/searxng.ts`
does `cleanedUrl + "/search"` — both append exactly one path segment and both
read `results[].{title,url,content,score}`.

For Degoog to answer `/api/search?format=json` in the SearXNG shape, the
instance setting `searxApiEnabled` must be on. It is off by default upstream and
is seeded by the `seed-engines` initContainer.

## Engines

Degoog ships **zero** search engines — a fresh instance returns nothing at all
until one is installed, which is upstream's intent. Because the primary consumer
is an agent whose empty-result failure is silent, engines are seeded at pod start
by an initContainer instead of waiting for the setup wizard:

- fetched from `degoog-org/official-extensions` at the pinned commit in
  `deployment.yaml` (`ENGINE_COMMIT`)
- `bing`, `brave`, `duckduckgo`, `wikipedia` → `data/engines/<id>/`
- `data/default-engines.json` pins those four ids on
- guarded by a stamp file, so it runs once per pinned commit and never overwrites
  a later install/uninstall done from the UI
- non-fatal: a failed fetch logs a warning and the pod still starts

To move engine code forward, bump `ENGINE_COMMIT` to the new
`degoog-org/official-extensions` `main` sha. The stamp filename contains the
commit, so a bump re-seeds on the next pod start.

Engines fetched this way are not tracked by the Store UI (they show as untracked
folders). That is cosmetic; adding the official repo in **Settings → Store**
gives the normal install/update buttons, and a Store install of the same engine
simply lands in the same directory.

If an engine starts returning nothing, its outgoing transport is the first knob —
the image ships `curl-impersonate`, so switching an engine from `fetch` to
`curl-impersonate` in the engine's Advanced settings is the documented fix for a
site that has started refusing non-browser clients.

## Not covered

- `ExternalSecret/degoog-secrets` reports `UpdateFailed: secrets "degoog-secrets"
  already exists` (as does `b2-credentials`). The Secret exists and the pod has its
  password, so this is not breaking anything today, but it means the secret is not
  owned by the ExternalSecret and a Key Vault rotation would not propagate. Same
  pre-existing pattern on the other ExternalSecret in this namespace; left alone
  here rather than churned as collateral.
- There is no monitor for the failure mode that actually matters here: an instance
that answers HTTP 200 with an empty result set. A plain HTTP probe would call it
healthy. The previous SearXNG instance failed exactly this way (every enabled
engine suspended/CAPTCHA'd, `results: []`, `success: true`). Worth adding a
result-count assertion later if the engines prove flaky.

## Credentials

Unchanged by this PR. `DEGOOG_SETTINGS_PASSWORDS` comes from the `degoog-secrets`
ExternalSecret in this directory, backed by Azure Key Vault key
`degoog-settings-passwords`. Degoog reads it from the environment only — there is
no settings UI and no on-disk file for it — and it cannot be dropped, because an
unlocked instance lets any visitor install extensions, which run code in the pod.
The env var is a comma-separated list, so the password must not contain a comma.

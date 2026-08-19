# infra/deploy

Deploys `origin/main` to a **shared** Hetzner box (2 vCPU / 3.7 GiB) that also
runs the `jitraa` stack. Nothing here is published to a host port; the only way
in is the shared edge proxy — see [`../edge/README.md`](../edge/README.md) for
the boundary contract this stack has to honour.

This box runs the **API and its database only** — the SPA is built and hosted
elsewhere and calls `api.fynai.co` cross-origin.

```
  SPA (off-box) ── fynai.co
        │  fetch, credentials: "include"
        ▼
  api.fynai.co ──> edge (Caddy, shared) ──> fyn-backend:8000 ──> fyn-postgres:5432
```

| Script | Use |
|---|---|
| `setup-server.sh` | One-time bootstrap. Idempotent; safe to re-run |
| `deploy.sh` | `[all\|api] [--no-cache] [--server]` — the deploy |
| `install-route.sh` | Ships `fyn.caddy` to the edge proxy, validates, reloads |
| `status.sh` | Live commit, containers, health, box memory |
| `logs.sh` | `[service] [tail]` — follow logs |

Every setting lives in `config.sh` and is env-overridable:

```bash
FYN_HOST=1.2.3.4 FYN_DOMAIN=staging.example.com ./infra/deploy/deploy.sh
```

Things worth knowing before touching any of this:

- **`docker-compose.prod.yml` is standalone, not an override.** Compose
  *appends* to `ports` when merging files, so an override cannot unpublish the
  root `docker-compose.yml`'s `0.0.0.0:5432/:8000/:3000`. On a shared box those
  must not be published. Keep the two in sync by hand when services change.
- **Migrations run inside the backend container** from its `CMD`. There is no
  separate migrate step; a failed migration surfaces as an unhealthy container.
- **No SPA build happens here.** That was the one workload big enough to OOM a
  3.7 GiB box and get *jitraa* killed. `frontend/Dockerfile` still honours a
  `NODE_OPTIONS` build arg for wherever the SPA is built.
- **The session cookie is host-only on the API** (`SESSION_COOKIE_DOMAIN=`
  empty). It survives the app/api split because both hostnames share the
  registrable domain `fynai.co`, making the pair same-site. Serving the SPA
  from an unrelated domain would force `SameSite=None`.
- **`backend/.env` on the server is not in git** and is never overwritten after
  the first seed.
- **The server clones through the `github-fyn` SSH alias**, not `github.com`, so
  fyn's read-only deploy key is not offered to anything else on the box.
- **No automated rollback.** `compose up -d` replaces the running containers.
  See the Rollback section in `.claude/skills/deploy/SKILL.md`.
- **`api.fynai.co` must stay DNS-only (grey cloud) in Cloudflare.** The orange
  proxy buffers and times out the agent's server-sent events, and grey is what
  lets Caddy use tls-alpn-01.

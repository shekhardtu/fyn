---
name: deploy
description: "Deploy `main` to the shared Hetzner box (49.13.87.106) via infra/deploy/deploy.sh — server pulls origin/main, rebuilds the Docker Compose stack, runs alembic on container start, reloads the shared edge proxy, verifies health. The box also runs jitraa, so read the boundary rules before touching anything. Triggers on \"deploy\", \"deploy to production\", \"ship it to the server\", \"/deploy\", \"redeploy\", and on asking why production is on an old commit."
---

# Deploy — `main` to the shared Hetzner box

Owns the `infra/deploy/deploy.sh` flow. The server only ever runs code from
`origin/main`: nothing is copied from the laptop, the box pulls. What is
deployed is therefore always what is on GitHub.

`/deploy` is **ungated** — invoking it *is* the deploy decision. Print the
Phase 0 summary and proceed; do not `AskUserQuestion` to confirm.

## When to Use

- `main` has moved and production should catch up
- A server-side change (env value, Caddy config) needs the stack restarted
- A previous deploy failed midway and needs a retry

**Do NOT use** `/deploy`:
- To ship an unmerged branch — the script refuses anything but `main`
- To push local code to the server — all code arrives via `origin/main`
- For first-time server setup — that is `infra/deploy/setup-server.sh`

## Scope Boundary

| Belongs to `/deploy` | Does NOT belong here |
|---|---|
| Pre-deploy summary (informational) | Merging a PR |
| Verifying local `main` == `origin/main` | Running the test suite |
| Running `infra/deploy/deploy.sh` | First-run server bootstrap (`setup-server.sh`) |
| Waiting out migrations, reading the failure | Writing the migration |
| Reloading edge with fyn's own site file | Changing edge itself, or jitraa's route |
| Health verification, public and internal | Debugging jitraa or the shared edge proxy |
| Reporting which commit is live | Rollback (see Rollback below) |

---

## The Deployment, in One Picture

```
  SPA (built and hosted OFF this box)  ── app.fynai.co
            │  fetch, credentials: "include"
            ▼
  api.fynai.co ──> edge-caddy-1  (shared: also serves jitraa's three hosts)
                     └─> fyn-backend:8000 ──> fyn-postgres:5432
                            (fyn_default — private)
```

Facts that follow from that shape, and that the failure table depends on:

- **This box runs the API and its database only.** There is no frontend
  container and no nginx `/api` relay. The SPA is built elsewhere and calls
  `api.fynai.co` cross-origin.
- **The session cookie survives that split** because `app.fynai.co` and
  `api.fynai.co` share the registrable domain `fynai.co`, which makes the pair
  same-site. `SESSION_COOKIE_DOMAIN` is deliberately **empty** — a host-only
  cookie on the API is the tightest scope that works. Moving the SPA to an
  unrelated domain would break this and force `SameSite=None`.
- **The box is shared with `jitraa`** (jitraa.com, admin., api.). Separate
  compose project, private network, SQLite volume. The boundary contract is in
  `infra/edge/README.md` — read it before changing anything outside `fyn`.
- **Nothing of fyn's is published to a host port.** The single public entrance
  is `edge-caddy-1`. A "port closed" reading from outside is correct.
- **Only `fyn-backend` joins the shared `edge` network**, under that alias.
  Postgres stays on `fyn_default`, where jitraa cannot resolve it.
- **`api.fynai.co` must stay DNS-only (grey cloud).** Cloudflare's proxy
  buffers the agent's server-sent events, and grey is also what lets Caddy use
  tls-alpn-01. jitraa learned this the same way for `api.jitraa.com`.
- **Migrations run inside the backend container**, from its `CMD`
  (`alembic upgrade head && uvicorn …`), on every start.
- **Every container is memory-capped** (fyn: 1G backend / 512M postgres).
  Caps across all projects sum to 2.5G of 3.7G. Do not raise one without
  re-checking that total.
- **Never restart edge; reload it.** A restart drops jitraa's connections too.
- **Never `docker system prune`.** It is not scoped to a project.

## Phase 0: Deploy Summary (Informational)

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  READY TO DEPLOY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Target:       shared Hetzner box — root@49.13.87.106:/opt/fyn  (jitraa also lives here)
Branch:       main
Latest SHA:   <sha> — <subject>
Migrations:   <Yes — list alembic/versions files new since the live SHA / None>
Scope:        <all | api>

Command:      ./infra/deploy/deploy.sh <target>
Health:       https://api.fynai.co/api/health

Steps:        ssh → git reset --hard origin/main
              → docker compose build
              → compose up -d  (alembic runs in the backend container)
              → install fyn.caddy on edge, validate, reload
              → wait for container health
              → health check, public then internal

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

To fill in "Migrations", diff the live SHA against local:

```bash
LIVE=$(ssh root@49.13.87.106 'cd /opt/fyn && git rev-parse HEAD')
git diff --name-only "$LIVE" HEAD -- backend/alembic/versions/
```

Then go straight to Phase 1.

---

## Phase 1: Pre-Deploy Checks

`deploy.sh` enforces all of these and exits non-zero with the reason, so the
value of doing them here is a better error message, not extra safety.

```bash
git -C /Users/hari/2026/expen status --porcelain   # must be empty
git -C /Users/hari/2026/expen rev-parse --abbrev-ref HEAD   # must be main
git -C /Users/hari/2026/expen fetch origin main && \
  git -C /Users/hari/2026/expen rev-parse HEAD origin/main   # must match
ssh -o BatchMode=yes root@49.13.87.106 true   # must succeed
```

If the working tree is dirty: **stop and ask**. Do not auto-stash. Unlike
yofix's flow this repo is routinely worked in with a large dirty tree, so a
blind stash risks burying real work.

If SSH fails, the key is not on the box. Fix (from the Hetzner console, as
root): `ssh-import-id-gh shekhardtu`. Do not attempt a password login;
`PermitRootLogin` on these boxes is `without-password`.

---

## Phase 2: Execute

```bash
./infra/deploy/deploy.sh          # all
cd /Users/hari/2026/expen && ./infra/deploy/deploy.sh api      # backend only
cd /Users/hari/2026/expen && cd /Users/hari/2026/expen && ./infra/deploy/deploy.sh --no-cache
```

**Do not pipe, background, or silence the output.** The build and migration log
is the whole diagnostic. Expect a couple of minutes on a cold `--no-cache`
build (`pip install` on 2 vCPU). The SPA is not built here.

### Failure Table

| Symptom in the output | Cause | Next step |
|---|---|---|
| `cannot SSH to root@…` | Key not on the box | Hetzner console → `ssh-import-id-gh shekhardtu` |
| `server-side git sync failed` | Deploy key expired/removed | Re-add a read-only deploy key on the repo; see `setup-server.sh` |
| `backend/.env is missing on the server` | Box never bootstrapped | Run `./infra/deploy/setup-server.sh` |
| `POSTGRES_PASSWORD` required | Server `.env` predates this infra | Add `POSTGRES_PASSWORD=…` to `/opt/fyn/backend/.env` — must match the one inside `DATABASE_URL` |
| `image build failed` | Type error, failed `npm ci`, dependency drift | Reproduce locally, fix, push, re-run. Nothing changed on the server |
| `backend reported unhealthy` + alembic traceback | Migration failed | **The old containers are already gone.** See Rollback. Read the traceback; a bad migration usually needs a follow-up migration, not a hand-edit of `alembic_version` |
| Healthy, but public URL unreachable and internal fine | DNS, not the app | `dig +short api.fynai.co` must return `49.13.87.106`, **DNS-only / grey cloud** — the orange proxy buffers SSE |
| `the edge config is invalid with fyn's route added` | Bad `infra/deploy/fyn.caddy` | The file was removed again and edge was NOT reloaded, so jitraa is untouched. Fix the site file |
| `the 'edge' network does not exist` | Shared ingress missing | `./infra/edge/install.sh` |
| Build killed with no error message | The box ran out of memory | `ssh root@… free -m`. The box is 3.7 GiB shared with jitraa |
| Deploy hangs at "building images" | Slow/cold build on 2 vCPU, or out of disk | Do not kill it. Check `ssh root@… 'df -h / && free -m'` in a second shell |
| jitraa went down during a fyn deploy | A boundary was crossed | Almost certainly an OOM or a `docker system prune`. `ssh root@… 'dmesg -T \| grep -i oom'` |

Do not auto-retry a failed migration or a failed build. Report and stop.

---

## Phase 3: Post-Deploy Verification

```bash
./infra/deploy/status.sh
```

That prints the live commit (flagging drift from `origin/main`), the `fyn-*`
containers with their health, and both health endpoints. Verify:

- the live commit equals the SHA you deployed
- every `fyn-*` container is `Up` — a `Restarting` backend means a crash loop
- `status` in the health JSON is `ok`; `degraded` means the operation catalog
  failed to load, which is a real regression worth chasing, not noise

If the deploy touched agent or streaming paths, watch a run's SSE for a minute:

```bash
./infra/deploy/logs.sh backend 100
```

Look for tracebacks, `RUN_ERROR`, and restart loops.

> **Known gap:** `/api/health` reports no version or commit, so the health
> response cannot by itself prove which code is live — `status.sh` uses the
> server's git SHA instead. Threading a `GIT_SHA` build arg into the image and
> adding it to `HealthOut` would make the health response self-proving. Not
> done; deliberate, and worth doing when someone next touches `HealthOut`.

---

## Rollback

There is no automated rollback, and it matters that this is understood before
it is needed: `compose up -d` replaces the running containers, so a failed
deploy does **not** leave the previous version serving.

To go back to a known-good commit:

```bash
ssh root@49.13.87.106 'cd /opt/fyn && git reset --hard <good-sha>'
./infra/deploy/deploy.sh --server
```

**A migration that already applied is not undone by this.** If the bad commit
migrated the schema, rolling the code back can leave the old code facing a
newer schema. Check whether the failed deploy's migrations ran
(`docker exec fyn-postgres-1 psql -U finance -d finance -c 'select version_num from alembic_version'`)
before assuming a code rollback is sufficient.

---

## Phase 4: Output Summary

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  DEPLOY COMPLETE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Commit:     <sha> — <subject>
Scope:      <all | api | web>
Migrations: <Ran N: names / None>
Duration:   <elapsed>
Health:     ok (https://api.fynai.co/api/health)
Containers: fyn-backend-1 Up (healthy), fyn-postgres-1 Up (healthy)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Skill: /deploy
File:  .claude/skills/deploy/SKILL.md
```

On failure use "DEPLOY FAILED" and include: the phase that failed, the trimmed
error, whether the schema was migrated, and whether anything is currently
serving. Never report a deploy as complete on the strength of the script's exit
code alone — Phase 3 is what confirms it.

---

## Files

| Path | Role |
|---|---|
| `infra/deploy/config.sh` | Host, paths, domain, build cap. Every value env-overridable |
| `infra/deploy/deploy.sh` | The deploy this skill runs |
| `infra/deploy/setup-server.sh` | One-time (idempotent) box bootstrap |
| `infra/deploy/install-route.sh` | Ships `fyn.caddy` to edge, validates, reloads |
| `infra/deploy/status.sh` | Live commit, containers, health, box memory |
| `infra/deploy/logs.sh` | Follow container logs |
| `infra/deploy/docker-compose.prod.yml` | fyn's production stack — standalone, not an override |
| `infra/deploy/fyn.caddy` | fyn's route on the shared proxy (`api.fynai.co`) |
| `infra/edge/` | **Shared** ingress project — jitraa depends on it too |

---

## Self-Healing

After every `/deploy`, re-read this skill against what actually happened:

| Check | Look for |
|---|---|
| Host / path | Is `root@49.13.87.106:/opt/fyn` still the target? |
| Domain | Does `api.fynai.co` still resolve to this box, DNS-only? |
| SPA origin | Does `CORS_ORIGINS` on the server still match where the SPA is served from? |
| Neighbours | Is `jitraa` still the only other project? A third app means edge moves to its own repo |
| Memory | Do the caps across all projects still sum under 3.7 GiB? |
| Services | Did a fourth service appear in `docker-compose.prod.yml`? |
| Compose drift | Does the root `docker-compose.yml` now have services the prod file lacks? |
| Failure modes | Any failure observed that the table above does not cover? |

Fix inaccuracies with a minimal `Edit` and log:

```text
Self-Healing Log:
- Fixed: <what was wrong> → <what it is now>
- Reason: <why it was wrong>
```

If nothing needs fixing, skip silently.

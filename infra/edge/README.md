# infra/edge — shared ingress for the box

One Caddy owns `:80`/`:443` and the certificate store for every application on
`49.13.87.106`. It knows no application logic: each app drops one file into
`sites/` and reloads.

```
                         :80 / :443
                              │
                   ┌──────────▼──────────┐
                   │  edge  (Caddy only) │   compose project: edge
                   │  /opt/edge/sites/*.caddy  ← one file per app, app-owned
                   │  volume edge_caddy-data   ← all certs, one ACME account
                   └───┬──────────────┬──┘
            net: edge  │              │  net: edge
              ┌────────▼───┐     ┌────▼─────────┐
              │ jitraa-web │     │ fyn-backend  │      ← ingress-facing only
              └──────┬─────┘     └──────┬───────┘
        net: jitraa_default        net: fyn_default    ← private, per app
              ┌──────▼─────┐            └──────┐
              │ backend    │            ┌──────▼───┐
              │ litestream │            │ postgres │
              └────────────┘            └──────────┘
```

## The contract

An application that wants a public hostname must:

1. **Join the external `edge` network with a service name that is unique
   across the whole box** — `jitraa-web`, `fyn-backend`. Not merely an explicit
   `aliases` entry: Compose registers the *service name* as an alias on every
   network the service joins, in addition to anything under `aliases`. A
   service called `backend` therefore publishes `backend` here, where it
   shadows the `backend` of every neighbour that has one. That is not
   hypothetical — it took jitraa's API down on 2026-08-19, twenty minutes after
   fyn's API joined this network as a service named `backend`: jitraa's web
   container resolved `backend` to fyn's, and got connection-refused on a port
   fyn does not listen on. Nothing in either app had changed.
2. **Put exactly one container on `edge`** — its own front door. Databases,
   queues and workers stay on the app's private default network, where no
   neighbour can resolve or reach them.
3. **Own exactly one file, `sites/<app>.caddy`.** Never edit another app's.
4. **Validate, then reload. Never restart.** A restart drops every app's
   connections; an unvalidated config that fails to parse takes the whole box's
   ingress down. Both app deploys do `caddy validate` and back the file out
   again if it fails.
5. **Cap memory.** The box is 2 vCPU / 3.7 GiB. An uncapped container that runs
   away gets *someone else's* process chosen by the kernel OOM killer.

Current caps — the sum stays under physical RAM, so they are a real guarantee
rather than an overcommit:

| container | cap |
|---|---|
| `edge-caddy-1` | 192M |
| `jitraa-backend-1` / `-litestream-1` / `-web-1` | 512M / 128M / 128M |
| `fyn-backend-1` / `-postgres-1` | 1G / 512M |
| **total** | **2.5G of 3.7G** |

## Files

| Path | Role |
|---|---|
| `docker-compose.yml` | The edge project. Installed to `/opt/edge` |
| `Caddyfile` | Global options and shared snippets, then `import sites/*.caddy` |
| `install.sh` | Idempotent installer; `--swap` performs the cutover |
| `sites/` | Empty in git — each app ships its own file from its own repo |

Snippets any app can import: `cloudflare_proxied` (for orange-cloud hosts,
which cannot complete tls-alpn-01) and `app_defaults` (compression + security
headers).

## Ownership note

This directory lives in the fyn repo because fyn was the second app onto the
box and there was nowhere better. It is **not fyn's** — jitraa depends on it
equally. When a third application arrives, move it to its own repo.

## History

Before 2026-08-19 there was no shared ingress: Caddy ran *inside* jitraa's
`web` image (`COPY deploy/Caddyfile /etc/caddy/Caddyfile`), owned the ports,
and held the only certificate store. Adding a hostname for any other app meant
editing and rebuilding jitraa. The split moved TLS to this project, copied the
certificates into `edge_caddy-data` (so Let's Encrypt was not re-issued against
its rate limits), and left jitraa's Caddy serving plain HTTP behind it with
`auto_https off`. Cutover took 6 seconds.

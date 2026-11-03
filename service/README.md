# Resource board service

The service collects read-only inventory over SSH, stores snapshots,
environment records, GPU allocations, and audit events, and serves the HTTP API
and web board.

仓库层面的三部分划分见 [`../docs/`](../docs/)：服务的代码、服务启动的约束、使用方法。本文件只保留服务自身的启动参数与接口清单。

## Start

```bash
uv sync
tailscale up
sudo tailscale set --operator=$USER   # once, so the service can manage serve config
service/scripts/adaptive-service \
  --state-dir ~/.adaptive-computation \
  --ssh-config ~/.ssh/config \
  --interval 60
```

The service publishes itself with `tailscale serve`. It listens only on
`127.0.0.1`, and tailnet devices reach it through the HTTPS endpoint that
Tailscale provides for this machine. Tailscale Serve is limited to tailnet
devices; this project never enables Funnel.

The service validates no bearer token because the board is read-only for
ordinary viewers and is reachable only by tailnet devices through
`tailscale serve`. Clients may set `ADAPTIVE_SERVER` when the default local
listener is not the right address.

The startup output shows the tailnet URL, for example
`https://<controller>.<tailnet>.ts.net/`. The local listener stays on
`127.0.0.1:8765`.

Use `--tailscale-port` when port 443 already serves another application:

```bash
service/scripts/adaptive-service --tailscale-port 8443
```

The board is then available at `https://<machine>.<tailnet>.ts.net:8443/`. A
conflicting proxy target on the same port stops startup with the existing
target. When the service exits, it removes its own serve rule.

Use `--no-tailscale` only for local development; that mode does not create a
serve rule and does not provide HTTPS.

## Local defaults

Real plan names, node aliases, and device indices belong in `service/local.json`
next to this file. The file is listed in `.gitignore` and must never be
committed; start from `service/local.example.json`. When the file exists, its
`default_plans` array is seeded on the first start instead of the committed
placeholder examples. `ADAPTIVE_SERVICE_LOCAL` points at another file. An
existing database keeps its plans because seeding runs once per database.

After a `git pull`, run `development/update.sh` to fetch the private config
repository and copy both `local.json` files into place. The startup line
`local plans:` reports which file was loaded.

## Build the board

```bash
npm ci --prefix service/frontend
npm run build --prefix service/frontend
```

The build output is written to `service/adaptive_service/static/` and served by
the service at `/`. The directory is ignored by git.

## systemd user service

The service runs under a systemd user unit so it starts on boot:

```ini
[Unit]
Description=Adaptive resource board service
After=network-online.target

[Service]
Type=simple
ExecStart=/absolute/path/to/AdaptiveComputation/service/scripts/adaptive-service --tailscale-port 8443
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now adaptive-service.service
```

## HTTP API

All endpoints are under `/api/v1` and require no bearer token. Only tailnet
devices can reach the service through `tailscale serve`:

- `GET /snapshot` — current snapshot with nodes, environment records,
  allocations, and policy
- `GET /snapshot/{id}` — a specific snapshot
- `GET /nodes`, `GET /nodes/{alias}`, `GET /nodes/{alias}/series`
- `GET /nodes/{alias}/work-path`, `POST /nodes/{alias}/work-path`
- `GET /nodes/{alias}/paths/{kind}`, `POST /nodes/{alias}/paths/{kind}` —
  `kind` is `work`, `data`, or `model`
- `GET /nodes/{alias}/directories/{kind}` — immediate child directories and
  sizes for a registered `data` or `model` path
- `GET /nodes/{alias}/uv-cache` — uv cache packages, Python versions, and project lock files
- `GET /nodes/{alias}/nvitop` — structured GPU and process state read through nvitop
- `GET /nodes/{alias}/nvitop/text` — one-shot nvitop terminal output
- `GET /nodes/{alias}/gpu-state` — per-GPU `idle` or `occupied` state; `idle`
  means every nvitop sample in the last 30 seconds used at most 1 GiB of GPU
  memory. Before a full window exists, such as right after a restart, the most
  recent sample decides
- `POST /inventory/refresh`, `GET /jobs/{id}`
- `GET /environments`, `POST /environments`
- `GET /allocations`, `POST /allocations`, `DELETE /allocations/{alias}/{gpu}`
- `POST /plans` — create a plan with `name`, `owner`, `scope` (`static` or
  `all`), and `devices`; `scope=all` follows every reachable GPU in the latest
  snapshot, while `static` keeps the listed devices
- `PUT /plans/{plan_id}` — replace the plan name, scope, and devices
- `GET /plans` — plan summaries with expanded devices, filterable by `status`
  and `owner`
- `GET /plans/{plan_id}` — plan summary with expanded devices
- `POST /plans/{plan_id}/claim` — check the devices and reserve them for
  `target`; returns 409 with per-device conflicts when a node is unreachable,
  a GPU is not idle in the last 30 seconds, or another user holds it
- `POST /plans/{plan_id}/release` — release the plan's allocations
- `POST /plans/{plan_id}/pause`, `POST /plans/{plan_id}/resume`
- `DELETE /plans/{plan_id}`
- `GET /audit`
- `GET /events` — server-sent snapshot version events

`GET /healthz` is unauthenticated. `GET /nvitop/{alias}` serves the full
terminal view opened from a node's nvitop link; the board itself refreshes the
structured nvitop data every five seconds.

## Tests

```bash
uv run python -m pytest -q service/tests
```

Inventory field details are in [`references/inventory-schema.md`](references/inventory-schema.md).

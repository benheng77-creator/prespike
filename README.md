# claw247-trading

Trading fork. Three deliverables live in this repo:

| Surface | Path | Tech |
|---|---|---|
| Python trading core | [openclaw_v1/](openclaw_v1/) | asyncio trader + FastAPI read-only API |
| Node control panel | [deploy/openclaw-panel/](deploy/openclaw-panel/) | live at `http://127.0.0.1:8787/p/<token>/` |
| Static dashboard | [web/](web/) | Cloudflare Pages — https://claw247-trading.pages.dev |
| Mobile clients | [apps/](apps/) | android · ios · macos · shared |

## Pinned toolchain

- Node **22.22.2** (see `.nvmrc` / `.node-version`) — enforced by [scripts/fork-doctor.mjs](scripts/fork-doctor.mjs)
- Python **3.12**
- pnpm 10

## Quick start — backend + dashboard

```bash
# Python trading API on :8080
pip install -r openclaw_v1/requirements.txt
cd openclaw_v1 && uvicorn server:app --port 8080

# Static dashboard on :8765 (in another terminal)
cd web && python -m http.server 8765 --bind 127.0.0.1
# open http://localhost:8765/
```

The dashboard auto-connects to `http://localhost:8080` when served on localhost.

## Gates

| Command | What it does |
|---|---|
| `pnpm run doctor` | Node pin + pkg identity + forbidden-path guard |
| `pnpm run check` | doctor + recursive pnpm check/lint |
| `pnpm run test` | doctor + recursive pnpm test |
| `pnpm run build` | doctor + recursive pnpm build |
| `pnpm run audit:prod` | `pnpm audit --prod` |
| `pnpm run verify` | all of the above in order |

Python side: `cd openclaw_v1 && python -m pytest tests/ -q`

## Deploys

- **Frontend (Pages):** pushes to `main` deploy to https://claw247-trading.pages.dev (wrangler project `claw247-trading`)
- **Backend (Fly):** `fly.toml` → app `claw247-trading`, Dockerfile at [openclaw_v1/Dockerfile](openclaw_v1/Dockerfile)
- **Backend (Render):** `render.yaml` — same Docker build

## Forbidden re-appearances

`fork-doctor.mjs` fails CI if any of these come back:
`Dockerfile` (root), `docker-compose.yml`, `openclaw.mjs`, `tsconfig.*.json`, `tsdown.config.ts`, `knip.config.ts`, `.oxlintrc.json`, `.oxfmtrc.jsonc`, `.pre-commit-config.yaml`, `.jscpd.json`, `openclaw_layer/`, root `vitest.*.config.ts`, `src/plugin-sdk/`, `src/video-generation/`.

They were upstream carryover. This fork doesn't build the openclaw TypeScript monorepo.

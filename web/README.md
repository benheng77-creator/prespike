# claw247 trading — static dashboard

Single-file static dashboard for the trading backend. Deployed to Cloudflare Pages.

## Deploy on Cloudflare Pages

1. Dashboard > Workers & Pages > Create > Pages > **Connect to Git**.
2. Select this repo, branch `main` (or `fix/fork-reality`).
3. Build settings:
   - Framework preset: **None**
   - Build command: *(leave empty)*
   - Build output directory: `web`
4. Save and deploy.

## Backend contract

The page calls four endpoints on whatever URL you type into the header input
(saved per-browser in `localStorage`):

| Method | Path         | Used for                      |
|--------|--------------|-------------------------------|
| GET    | `/status`    | key/value rows in status card |
| GET    | `/positions` | pretty-printed JSON           |
| GET    | `/trades`    | pretty-printed JSON           |
| GET    | `/gate`      | pretty-printed JSON           |

All must return JSON and set `Access-Control-Allow-Origin` to the Pages URL
(or `*`) so the browser can read them.

The backend shim is [openclaw_v1/server.py](../openclaw_v1/server.py). It reads
the same SQLite file `TradeLogger` writes (`trades.db` by default, override
with `TRADE_DB_PATH`). Deploy to Fly using the repo's [fly.toml](../fly.toml)
or to Render using [render.yaml](../render.yaml).

## Run locally

```
pip install -r openclaw_v1/requirements.txt
cd openclaw_v1 && uvicorn server:app --port 8080
# then open web/index.html and paste http://localhost:8080 in the backend field
```

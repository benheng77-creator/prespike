# Moomoo / Futu integration (additive)

Moomoo is **not** a CCXT exchange. Verify locally:

```bash
python -c "import ccxt; print('moomoo supported =', 'moomoo' in ccxt.exchanges); print([x for x in ccxt.exchanges if 'moo' in x])"
```

Result on this project (ccxt 4.5.48):

```
moomoo supported = False
matches: []
```

So `EXCHANGE_ID=moomoo` in `openclaw_v1/config/.env` will fail at import. The
Moomoo stack is documented separately under **Moomoo API / OpenD / SDK** and
requires its own adapter.

## What this integration adds

A single new file alongside the existing CCXT adapter. Nothing existing is
modified:

- `core/exchange_moomoo.py` — `MoomooExecutionEngine` drop-in replacement
  for `core.exchange.ExecutionEngine`. Speaks Moomoo OpenD via the
  `moomoo-api` SDK and presents the same async interface (`watch_trades`,
  `watch_order_book`, `fetch_ohlcv`, `watch_ohlcv`, `create_order`) that
  `main.py` already calls on the CCXT exchange object.

The panel's **Settings → API keys & integrations** card also has a
"Moomoo / Futu (OpenD + OpenAPI)" credentials group for the 10 relevant
env vars.

## What you still need to do

### 1. Install the SDK

```bash
cd openclaw_v1
. venv/Scripts/activate  # or venv/bin/activate on Linux/Mac
pip install moomoo-api
```

(The package is also published as `futu-api`; `exchange_moomoo.py` tries
both imports.)

### 2. Install and run OpenD

OpenD is the local daemon that bridges your Moomoo account to the SDK.
Download from <https://openapi.futunn.com>, log in with your Moomoo
credentials, and start it so it listens on `127.0.0.1:11111`.

### 3. Fill in the credentials

In the panel's **Settings → API keys & integrations** card, or by editing
`openclaw_v1/config/.env` directly:

| Key                                                   | Purpose                                                     |
| ----------------------------------------------------- | ----------------------------------------------------------- |
| `MOOMOO_HOST`                                         | OpenD host, default `127.0.0.1`                             |
| `MOOMOO_PORT`                                         | OpenD port, default `11111`                                 |
| `MOOMOO_TRADE_PASSWORD` **or** `MOOMOO_TRADE_PWD_MD5` | trade unlock password (MD5 form preferred)                  |
| `MOOMOO_ACCOUNT_ID`                                   | optional; the SDK picks the first eligible account if blank |
| `MOOMOO_MARKET`                                       | `HK`, `US`, `CN`, or `SG`                                   |
| `MOOMOO_TRADE_ENV`                                    | `REAL` or `SIMULATE`                                        |
| `MOOMOO_RSA_PRIVATE_KEY`                              | optional, for encrypted OpenD protocol                      |
| `MOOMOO_OPENAPI_KEY` / `_SECRET`                      | only for cloud OpenAPI                                      |

### 4. Wire the adapter into `main.py`

`main.py` is **not** changed by this integration. When you are ready to
actually route through Moomoo, apply this one-line swap yourself:

```python
# openclaw_v1/main.py, around line 315

# before
from core.exchange import ExecutionEngine
engine = ExecutionEngine(config.exchange.id, api_key, secret)

# after
if config.exchange.id == "moomoo":
    from core.exchange_moomoo import MoomooExecutionEngine
    engine = MoomooExecutionEngine()
else:
    from core.exchange import ExecutionEngine
    engine = ExecutionEngine(config.exchange.id, api_key, secret)
```

Then set `exchange.id: moomoo` in `config/openclaw.yaml` (and use Moomoo
symbol format like `HK.00700` or `US.AAPL` instead of `BTC/USDT`).

## Notes on differences from CCXT

| Concern           | CCXT                         | Moomoo                                                       |
| ----------------- | ---------------------------- | ------------------------------------------------------------ |
| Symbol format     | `BTC/USDT`, `ETH/USDT`       | `HK.00700`, `US.AAPL`, `SZ.000001`                           |
| Trade asset class | spot / perp / futures crypto | HK/US/CN/SG equities, ETFs, options                          |
| Connection model  | HTTP + WebSocket to exchange | **Local daemon (OpenD)** + SDK callbacks                     |
| Live data shape   | async iterator (`ccxt.pro`)  | callback-based; wrapped here with `asyncio.Queue`            |
| Auth              | `apiKey` + `secret`          | login via Moomoo app → OpenD; trade requires unlock password |
| Paper trading     | per-exchange flag            | `MOOMOO_TRADE_ENV=SIMULATE`                                  |

## Why the adapter is a file, not a switch in `core/exchange.py`

Strict additive integration. The existing CCXT `ExecutionEngine` is
untouched so none of the tests (`test_*.py` under `openclaw_v1/tests/`)
can regress. When you decide to wire it in, you do it yourself — the
adapter is ready and the credentials form in the panel is already in place.

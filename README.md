# market-orchestrator

Python + Node.js based monorepo for automated trading operations with a broker-agnostic architecture.

## Vision

- Run scheduled jobs every 1 minute / 5 minutes for account checks, market scans, and order workflows.
- Start with Korea Investment & Securities (KIS) Open API, but keep broker integrations pluggable.
- Provide safety-first controls such as forced stop-loss sell and emergency stop.
- Offer a web/app UI for monitoring balances, positions, strategy signals, and execution logs.

## Runtime Baseline

- Python: `3.11.2`
- Node.js: `v22.22.2`

## Monorepo Layout

- `apps/web`: Vite frontend dashboard (React + TypeScript)
- `apps/mobile`: mobile/desktop client placeholder
- `services/scheduler`: 1m/5m orchestration jobs
- `services/trading-bot`: strategy execution, risk guard, order flow
- `packages/broker-core`: broker interfaces and domain contracts
- `packages/broker-kis`: KIS Open API adapter
- `packages/shared`: shared config, logging, utilities
- `docs`: GitHub Pages source and operation docs

## Secrets and Configuration

This repository is designed so any user can clone and run by providing only their own secrets.

1. Copy `.env.example` to `.env`.
2. Fill in your own credentials.
3. Never commit `.env`.

Required keys are documented in `.env.example`.

## Quick Start

```bash
# frontend
cd apps/web
npm install
npm run dev
```

## Auto Floor Sell (KIS)

`services/trading-bot/auto_floor_sell.py` implements automated floor sell by provider branch.

- Input
  - `--sell-ratio` (default `0.10`)
  - `--config` account config JSON path
- Current provider support: `kis`
- Execution guard
  - It checks market open status on every run.
  - If market is closed, sell logic is skipped.

### Account Config JSON Example

Use `services/trading-bot/account.example.json` as template.

### Run Manually

```bash
python services/trading-bot/auto_floor_sell.py \
  --config services/trading-bot/account.json \
  --sell-ratio 0.10 \
  --dry-run
```

Read-only test (ignores market-open gate and never sends orders):

```bash
python services/trading-bot/auto_floor_sell.py \
  --config services/trading-bot/account.json \
  --sell-ratio 0.10 \
  --read-only
```

### Cron Schedule (15m, KST, Mon-Fri 09:00-15:00)

Use `services/scheduler/crontab.example` as reference.

```bash
crontab -e
```

Then register the cron line from the example file.

## GitHub Pages

`docs/` is prepared as the source for GitHub Pages.
After Pages is enabled with `branch=main` and `path=/docs`, the site is published from `docs/index.html`.

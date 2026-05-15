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

## GitHub Pages

`docs/` is prepared as the source for GitHub Pages.
After Pages is enabled with `branch=main` and `path=/docs`, the site is published from `docs/index.html`.

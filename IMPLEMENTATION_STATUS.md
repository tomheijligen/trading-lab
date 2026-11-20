# Trading_Lab implementation status

Branch: `feature/global-paper-trading`

This checkpoint continues from commit `889a0d5`. No changes have been pushed or merged. The original PostgreSQL database was not migrated during this checkpoint.

## Implemented

- Preserved the existing ten strategies, virtual accounts, users, balances, positions, trade history and account ownership rules.
- Created a separate recovery point containing the pre-change PostgreSQL dump and original working-tree patch.
- Added persistent market quote caching, provider usage counters, backoff, historical daily bars and data freshness checks.
- Integrated Twelve Data, Binance public Spot candle REST, optional Binance WebSocket candles and Alpaca IEX as a research-only fallback for supported US listings.
- Added an explicit global scanner watchlist, configurable momentum, trend and volatility ranking, quote age, source and currency display, and a staff-only provider usage page.
- Added atomic bot switching, retained positions, virtual liquidation with eligible fresh quotes, positions held outside the active bot, account risk limits and switch history.
- Added queued historical simulations for all ten strategies, equal starting cash, fees, estimated spread and slippage, equity curves, drawdown, win rate, Sharpe estimates, JSON export and walk-forward evaluation. Results remain separate from paper accounts.
- Added matched paper comparisons when suitable start and end marks exist without deposits or bot changes in the comparison period.
- Added recorded portfolio performance windows, a headless worker, healthchecks, Docker Compose, PostgreSQL persistence and Caddy HTTPS preparation.
- This checkpoint adds stale-round suppression, retries without duplicate entries, avoids duplicate stored bars when history is unchanged, and tests concurrent shared quote requests on PostgreSQL.

## Verification

- SQLite suite: `python manage.py test --settings=config.test_settings` — 31 tests passed, 1 PostgreSQL-specific test skipped.
- Isolated PostgreSQL suite: `python manage.py test trading.test_bot_control trading.test_concurrency` — 8 tests passed.
- The prior full suite on isolated PostgreSQL passed 31 tests.
- `python manage.py makemigrations --check --dry-run` reported no migration drift.
- The recovery dump was restored to a separate PostgreSQL database. Migrations 0010–0013 ran there. Existing row counts and content hashes for users, accounts, instruments and trades were unchanged.
- Docker Compose configuration was validated. A Docker image build and live HTTPS deployment have not been run.

## Remaining work and limitations

- Finnhub, Alpha Vantage and CoinGecko adapters remain unimplemented. Check provider licensing, regional coverage and display or redistribution rights for each selected plan.
- The scanner needs a manually configured watchlist. Global symbol discovery, a market-hours and holiday calendar, historical FX conversion, comparable bid/ask and liquidity metrics, and ETF expense-ratio and tracking data remain incomplete.
- Alpaca IEX has limited feed coverage and does not authorize execution. Existing engine support and available quotes constrain tradable instruments.
- Historical comparisons use stored EUR history from the selected account. They do not reconstruct a point-in-time global universe, delistings, dividend-adjusted total returns, historical FX or realistic order-book fills. Survivorship and universe bias remain possible.
- Walk-forward tests require adequate histories. Portfolio sizing and allocations are simplified.
- Paper performance is estimated from saved marks. Bot-period profit reports realized order profits. A per-bot equity ledger for carried positions has not been implemented.
- Resilience tests cover selected restart and concurrency cases, not every possible process interruption at every database and provider boundary.
- Docker deployment, SMTP, production secrets, encrypted off-host backups, restore drills, monitoring and alert delivery need operational setup.
- VPS sizing for 10, 50 and 100 accounts is estimated and has not been benchmarked. Provider quotas may bind before CPU or memory.
- Options are not traded. Real broker orders are not enabled.

## Providers and credentials

- Twelve Data requires `TWELVE_DATA_API_KEY`. Basic currently lists 8 credits/minute and 800 credits/day, subject to endpoint weights and market entitlements.
- Binance public Spot data requires no key. Optional stream symbols are configured with `MARKET_BINANCE_STREAM_SYMBOLS`.
- Alpaca research fallback requires `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`. The free Basic equity feed is IEX and does not authorize orders.
- Verify vendor terms, entitlements, rate limits and redistribution rules before enabling public access.

## Resume instructions

1. Continue on `feature/global-paper-trading` and check `git status`.
2. Install `requirements.txt` using Python 3.11 or 3.12.
3. Set Django and PostgreSQL environment variables from `.env.example`. Use `TRADING_LOCAL=1` with an explicitly configured local development database.
4. Run `python manage.py migrate --noinput` only against a disposable or backed-up development database. Do not rehearse migrations on the original database.
5. Set `PLATFORM_LAB_ENABLED=1` to enable the research, simulation and risk pages.
6. Run `python manage.py test --settings=config.test_settings`. For PostgreSQL coverage, point `python manage.py test` at a separate PostgreSQL test server and a role allowed to create test databases.
7. Start the site with `python manage.py runserver 127.0.0.1:8000`. Run `python manage.py run_worker paper` and `python manage.py run_worker simulation` in separate terminals with the same environment.
8. Configure `MARKET_SCANNER_UNIVERSE` before running `python manage.py refresh_scanner`. Binance streaming needs `MARKET_BINANCE_ENABLED=1`, configured symbols and `python manage.py run_market_stream`.
9. For a later isolated Linux host, protect `.env`, configure HTTPS and SMTP, validate with `docker compose config --quiet`, then deliberately build and start Compose. No deployment has been performed.

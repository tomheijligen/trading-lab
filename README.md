# Trading Lab

Trading Lab is a Django and PostgreSQL application for testing and comparing
trading strategies with virtual money. Each user can create multiple virtual
accounts, give each account different rules, and review its positions, signals,
orders, and performance.

The current version runs locally on Windows. It supports virtual orders for
stocks, ETFs, and crypto. Options history can be imported for analysis. Real
accounts are a manual deposit register.

## Quick start for users

1. Sign in and select **+ New account**.
2. Choose **Virtual Trading**, enter a name and starting balance, and select a
   strategy in **Bot**. Use a name that identifies the strategy, such as
   `Mean Reversion - 5000 EUR`.
3. Set the allocation targets. For example, use 33% stocks, 33% ETFs, 33% crypto,
   and 0% options. This leaves 1% unallocated. The targets can add up to 100%.
   They guide new purchases. Actual allocation can differ as prices move and
   orders are executed.
4. Choose how the bot gets its instruments:
   - On the account's **Overview**, select **Enable automatic discovery** to let
     the bot pick from a small rotating list in asset classes with allocated
     capital. The bot continues to follow open positions.
   - On **Market**, select **Add instrument** to enter a ticker or crypto pair
     yourself. Enter the exchange where applicable. Manual instruments can be
     used alongside automatic discovery.
5. On **Market**, use **Edit stops** if you want to set a fixed or trailing
   stop-loss for an instrument. A setting of `0` disables the corresponding
   stop.
6. Select **Market ? Run virtual bot** to test one round. The bot fetches market
   data, calculates signals, and can execute virtual orders. The application
   returns to **Overview** after the round.

The host needs a working Twelve Data API key and access to the selected
instruments for market-data rounds.

### Enable automatic virtual trading

Automatic trading has two parts: the user enables the account in Trading Lab,
and the host runs the background scheduler.

| Setting or action | What it does |
|---|---|
| **Enable automatic discovery** | Lets the bot choose instruments to evaluate. |
| **Enable scheduled rounds** | Includes this account in the host's scheduled bot runs. |
| **Run virtual bot** | Starts one round for this account immediately and can book virtual orders. |
| **Refresh market data** | Updates quotes and signals without placing orders. |
| **Pause scheduled rounds** | Excludes this account from future scheduled rounds. |

To enable scheduled trading as a user:

1. Open your virtual account and go to **Overview**.
2. Choose automatic discovery or configure a manual watchlist.
3. Click **Enable scheduled rounds**. When the button changes to
   **Pause scheduled rounds**, the account is enabled.
4. Wait for the host's next scheduled run. A running scheduler picks up newly
   enabled accounts on its next round.
5. Reload **Signals** to see the latest execution outcome. Open **Overview ?
   Open positions** for current holdings and **Executed trades** for the order
   history.

The browser can be closed while the bot runs. The dashboard displays stored
data and needs a page reload to show changes from a background round.

`0 simulated orders` can be a normal result. A `BUY` or `SELL` signal is the
strategy's decision. **Execution status** explains whether an order was
placed or why it was blocked. A `SELL` signal with no position to sell does
not create a short position.

## Dashboard guide

### Main navigation

| Tab | What it shows and what you can do |
|---|---|
| **Overview / All accounts** | View the account list and combined performance charts for your virtual accounts. Open an account or create a new one. |
| **Virtual Trading** | Find your virtual accounts and their strategies. Open one to manage its portfolio and bot settings. |
| **Market** | Find the market pages for your virtual accounts. Open an account to inspect its instruments, quotes, and signals. |
| **Signals** | Compare stored signals across your virtual accounts. View the instrument, strategy reason, quote time, execution status, and last evaluation time. Opening this page does not request new market data. |
| **Reports** | Open a virtual account's period reports or go to **Compare strategies**. |
| **Real Trading** | View real account records and manually reported deposits. Broker execution is unavailable. |

The **left sidebar** links to your own trading accounts. For virtual accounts,
the percentage compares current value with starting capital plus booked
deposits. A deposit does not count as profit. Real accounts show no calculated
return because a broker balance is unavailable.

### Inside a virtual account

| Page or section | What it shows and what you can do |
|---|---|
| **Overview** | See total value, available trading cash, reserves, profit excluding deposits, and allocation by asset class. Toggle scheduled rounds or automatic discovery. |
| **Open positions** | See instruments currently held, units, purchase cost, latest stored value, and unrealized result. Unrealized results exclude future selling fees. |
| **Performance** | **Capital history** plots saved account values, including cash, reserves, and positions. **Realized result** plots the cumulative result after fees from closed positions. |
| **Executed trades** | Review each recorded `BUY` or `SELL`, including execution time, price per unit, units, fees, and the bot's reason. A buy does not yet have a realized result. |
| **Deposits** | Choose **Trading cash** to make a deposit available to the bot or **Reserved cash** to hold it outside the buying budget. |
| **Market** | Add an instrument, view daily closing prices, inspect the latest stored quote and strategy signal, or edit fixed and trailing stops. You can also refresh market data or run a virtual bot round. |
| **Reports** | Choose Daily, Weekly, Monthly, Quarterly, Six months, or Yearly. Reports show orders, winning and losing exits, break-even exits, and realized result after fees. Expand **Order explanations** for individual trades. |
| **Email reports** | Save the frequencies you want emailed to your registered address. A separate scheduled command must process due reports. |
| **Account settings** | **Delete account** opens a confirmation page. Confirming permanently deletes that trading account and its related positions, trades, deposits, snapshots, and email report preferences. |

### How to read the charts and signals

- **Period** chooses how far back the chart looks. **Detail** groups the stored
  points by minute, hour, day, week, or month. Choose **Original measurements**
  to view all saved points in the selected range.
- A single capital snapshot appears as one point. Changing the detail level
  does not create new observations or fill gaps. The combined capital chart
  begins once each included virtual account has a recorded value.
- Deposits can move the **Capital history** line. Check **Profit excluding
  deposits** for the current result relative to contributed capital.
- The **Realized result** chart needs at least one recorded sale. Open positions
  can gain or lose value before any result is realized.
- **Signal** is the latest stored strategy analysis. **Execution status** is
  the outcome of the last trading round that evaluated the instrument.
  Refreshing market data can update a signal while leaving the earlier
  execution status in place. Use **Last evaluated** to tell them apart.

### Strategy comparison and Learning Lab

Open **Reports ? Compare strategies** to compare your virtual accounts,
review executed orders across accounts, and see which instruments produced
winning or losing exits.

For a fair side-by-side test, give the accounts the same starting capital,
allocation targets, stop settings, fixed watchlist, and test period. Automatic
discovery can lead to different sets of instruments after the bots open
different positions.

Select **Run learning test** to replay the existing strategies on stored daily
prices from one account. The Learning Lab selects a candidate using an earlier
period and tests it on later dates. It may recommend another virtual test if
the candidate also improves on the later period. It does not place orders or
change an active strategy. The replay tests strategy signals and does not
reproduce all account allocations, quote restrictions, or stop-loss behavior.

### Profile, registration, and administration

- Select your **name or photo** in the header to open **Your profile**. You can
  change your display name, upload or remove a photo, and sign out.
- **Register** creates a regular user login. In local development, Django prints
  the email confirmation link in the terminal running the web server. Open
  the link and confirm the account before signing in.
- **Administration** opens Django's staff area when the route is enabled.
  Administrators can manage users there. A regular user manages only their
  own trading accounts through Trading Lab.

## Included strategies

| Strategy | Signal idea |
|---|---|
| **Trend** | Compare the latest close with a 20-day moving average. |
| **Trend Crossover** | Compare a 10-day average with a 30-day average. |
| **Momentum** | Compare the latest close with the close ten observations earlier. |
| **Breakout** | Look for a break above the previous 20 closes or below the previous 10 closes. |
| **Mean Reversion** | Look for a close at least 3% below the prior 20-day average, then an exit at or above the average. |
| **RSI Reversal** | Use a simple RSI over 14 price moves, with 30 for entry and 70 for exit. |
| **Volatility-Filtered Trend** | Use a 20-day trend rule with a 2% cap on the average absolute daily move for entry. |
| **Trend Pullback** | Look for a recovery above a short average while the close is above the longer average. |
| **Up-Day Count** | Look for at least seven moves in the same direction out of the last ten. |
| **Buy & Hold** | Buy when eligible and hold. Configured stops can still trigger a sale. |

All strategies use the same virtual execution engine. The simulator uses a
0.2% fee per order. Strategy signals use completed daily closes. Virtual
execution uses a recent quote, subject to the account's cash, allocation, and
position rules.

A quote must be no more than 30 minutes old. For stocks and ETFs, the provider
must also report that the market is open. New purchases currently require
eligible quotes for every selected instrument in the account. This can delay
a crypto buy in a mixed account when the stock market is closed. Eligible
sales can still be evaluated per instrument. Stop-loss rules are checked
during bot rounds, so they can miss price moves between rounds.

## Local setup for the host

### 1. Prepare Windows and PostgreSQL

Install Python 3.11 or newer and PostgreSQL 14 or newer. For Visual Studio
2022, enable the **Python development** workload and open the project with
**File ? Open ? Folder**.

In pgAdmin, create a login role named `trading_app`. Enable login and leave
superuser, role creation, and database creation privileges disabled. Create
a database named `trading_lab_v2` with `trading_app` as its owner.

### 2. Install the project and set local credentials

In Developer PowerShell, run the following from the folder containing
`manage.py`. If `env` already exists, skip the first command.

```powershell
py -3 -m venv env
.\env\Scripts\python.exe -m pip install -r requirements.txt

$env:TRADING_LOCAL = '1'
$env:PGHOST = '127.0.0.1'
$env:PGPORT = '5432'
$env:PGDATABASE = 'trading_lab_v2'
$env:PGUSER = 'trading_app'

$dbPassword = Read-Host 'PostgreSQL password for trading_app' -AsSecureString
$env:PGPASSWORD = [System.Net.NetworkCredential]::new('', $dbPassword).Password

if (-not $env:DJANGO_SECRET_KEY) {
    $env:DJANGO_SECRET_KEY = .\env\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(64))"
}
```

These settings apply to the current terminal and processes it starts. Keep
your database password, market-data key, and Django secret outside Git.
If you generate a different Django secret in a new session, users may have
to sign in again.

To enable quotes and bot rounds, enter your own Twelve Data key in the same
terminal:

```powershell
$marketKey = Read-Host 'Twelve Data API key' -AsSecureString
$env:TWELVE_DATA_API_KEY = [System.Net.NetworkCredential]::new('', $marketKey).Password
```

### 3. Apply migrations and start Django

Set `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` and `PGPASSWORD` explicitly. Review the database target
before migration. The following example intentionally selects the original local
database. Run the migration only after taking a backup and approving that target.

```powershell
.\env\Scripts\python.exe manage.py check
$env:TRADING_MIGRATION_TARGET = '127.0.0.1:5432/trading_lab_v2'
.\env\Scripts\python.exe manage.py migrate --plan
.\env\Scripts\python.exe manage.py migrate
Remove-Item Env:TRADING_MIGRATION_TARGET
.\env\Scripts\python.exe manage.py createsuperuser
.\env\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

The migration command compares the approved host, port and name with Django's configured target before
connecting. It then verifies the server's database name, login role and port before planning or applying
migrations. Compose's migration service reads `TRADING_MIGRATION_TARGET` from the Compose environment.
For the example development database the target is `db:5432/trading_lab_dev`. Use
`docker compose --env-file .env.example config --quiet` with `TRADING_ENV_FILE=.env.example` for offline validation.

Run `createsuperuser` once when setting up a new database. For an existing
database with an administrator, skip that command.

- [Dashboard](http://127.0.0.1:8000/)
- [Register](http://127.0.0.1:8000/register/)
- [Administration](http://127.0.0.1:8000/admin/)
- [Signals](http://127.0.0.1:8000/signals/)
- [Strategy comparison](http://127.0.0.1:8000/reports/strategies/)

### 4. Start the background scheduler

`run_bots` processes one round for all enabled virtual accounts. To run one
round from a terminal with the settings above:

```powershell
.\env\Scripts\python.exe manage.py run_bots
```

For repeating rounds on Windows, stop the development server with `Ctrl+C`
and use the **same PowerShell session**. Start the following job once. Change
`D:\Trading_Lab` if your project is in another folder.

```powershell
Start-Job -Name 'TradingLabBots' -ScriptBlock {
    Set-Location -LiteralPath 'D:\Trading_Lab'

    while ($true) {
        .\env\Scripts\python.exe manage.py run_bots
        Start-Sleep -Seconds 1800
    }
}
```

The first round starts immediately. Each subsequent round starts 30 minutes
after the previous one finishes. Start Django again in the same terminal:

```powershell
.\env\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

The scheduler checks enabled accounts on every round. You can enable or pause
accounts from the dashboard without restarting the job. Keep the PowerShell
session open and the computer awake. The job does not restart after Windows
restarts.

To inspect the job, stop the web server with `Ctrl+C` if it is occupying the
terminal. Run these commands from the original PowerShell session:

```powershell
Get-Job -Name 'TradingLabBots'
Receive-Job -Name 'TradingLabBots'
```

- `Running` means the job is processing a round or waiting for the next one.
- `Scanned: ...` lists the instruments the bot attempted to inspect.
- `quotes received` shows how many quotes were retrieved for the account.
- `simulated orders` shows how many orders were booked.
- `Receive-Job` clears the displayed output from the job buffer. Use `-Keep`
  if you need to read those lines again.

Accounts requesting the same instrument within a scheduled round share a
market snapshot. Different instruments and manual refreshes can still use
extra API credits. Manual quote refreshes and bot rounds share a two-minute
cooldown per account.

Restart the website with `runserver` after checking the logs. To stop the
background job itself, run:

```powershell
Stop-Job -Name 'TradingLabBots'
```

### 5. Process scheduled report emails

Email preferences are stored per account. The bot scheduler above processes
trades only. To process due reports, run this separately:

```powershell
.\env\Scripts\python.exe manage.py send_reports
```

A scheduler must run this command periodically for automatic delivery. In
`TRADING_LOCAL=1`, the console email backend prints outgoing messages to the
terminal. Actual inbox delivery requires a configured email backend.

## Daily historical OHLCV backfill

By default the command prints an offline plan. It does not connect to the database or contact a provider. Pin `--end` for a reproducible run. Use `--days 30|90|180|365` or an explicit inclusive `--start` and `--end`. The plan also includes the 30 expected sessions before the start date.

```powershell
python manage.py backfill_historical_data --provider binance --asset-class crypto --symbol BTC/EUR --exchange BINANCE --currency EUR --days 90 --end 2025-12-29
python manage.py backfill_historical_data --dry-run --provider twelve --asset-class stock --symbol AAPL --exchange XNAS --currency USD --adjustment split --start 2025-11-01 --end 2025-12-29 --calendar-file .\nasdaq-sessions.json
```

XNAS, XNYS, XAMS and XBRU generate rule-based sessions through 2030 with the pinned `exchange-calendars` package. Historical processing accepts automatic dates through the confirmed venue cutoff. That cutoff is December 2026 for XNAS, December 2028 for XNYS, and December 23, 2027 for XAMS and XBRU pending exact year-end hours. Nasdaq follows the library's XNYS schedule alias and retains its own XNAS series identity. Binance uses daily UTC sessions. Requests extending through the venue's current day or later are evaluated through yesterday. Check `requested_end` and `end` in the output. [Nasdaq calendar](https://www.nasdaqtrader.com/Trader.aspx?id=Calendar) · [NYSE calendar](https://www.nyse.com/trade/hours-calendars) · [Euronext 2027 calendar](https://www.euronext.com/en/media/16439/download)

Use `--calendar-file` to override an equity or ETF schedule, to cover unsupported years, or to supply a schedule for another listing MIC. Use the venue's published calendar. The inclusive `start` and `end` must cover the requested period and preceding warm-up. Supply one entry per session with its actual close as an offset-aware timestamp, including early closes. Omit weekends, holidays and other closures. The abbreviated example below shows the format. A schedule for a real request must include all applicable sessions.

```json
{
  "exchange": "XNAS",
  "timezone": "America/New_York",
  "start": "2025-12-24",
  "end": "2025-12-26",
  "sessions": {
    "2025-12-24": "2025-12-24T13:00:00-05:00",
    "2025-12-26": "2025-12-26T16:00:00-05:00"
  }
}
```

Crypto uses daily UTC sessions. Twelve Data uses bounded `time_series` requests. Alpaca uses the IEX feed and follows page tokens within each bounded request window. The supplied Alpaca MIC identifies the listing calendar, while IEX identifies the source feed. Binance verifies both assets of the Spot pair using `exchangeInfo` and uses UTC-bounded `klines` requests. Forming candles are excluded. [Twelve Data date boundaries](https://support.twelvedata.com/en/articles/5214728-getting-historical-data) · [Alpaca historical bars](https://docs.alpaca.markets/us/reference/stockbars) · [Binance Spot klines](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market)

`--apply` authorizes network requests and writes to `HistoricalBar`, the shared provider cache and quota counters. Use it only on the intended database with the historical schema already present and with authorization for the provider data. A rerun with the same dates skips windows already covered by valid bars. Partial responses leave gaps visible. Provider errors stop the command after reporting progress, and a temporary request failure gets one retry after the cache lease. A short-lived cached partial response can affect an immediate rerun. Existing source bars are preserved.

## Daily historical FX

`HistoricalFXRate` stores one canonical EUR/USD rate per provider and Sydney session date, with a modeled availability timestamp, quality status and ingestion timestamp. Migration `0015_historical_fx_rate` adds that table. USD/EUR is the reciprocal of the same row. A changed rate for a stored session is reported as a conflict and the original is retained.

The Twelve Data adapter uses daily `time_series` closes. The offline plan needs no connection or credentials. With authorization, `--apply` uses the existing `TWELVE_DATA_API_KEY`, provider cache, quota and backoff, and requires the FX schema on the intended database. The inclusive date range is limited to 365 calendar days. [Twelve Data FX timezone](https://support.twelvedata.com/en/articles/5745849-timezones) · [Historical date bounds](https://support.twelvedata.com/en/articles/5214728-getting-historical-data)

```powershell
python manage.py backfill_historical_fx --pair USD/EUR --start 2025-10-01 --end 2025-10-31
python manage.py backfill_historical_fx --dry-run --pair EUR/USD --start 2025-10-01 --end 2025-10-31
```

For future simulations, call `convert_historical_fx(amount, "USD", "EUR", as_of=valuation_timestamp)` from `trading.historical_fx`. The timestamp must include a UTC offset. The service returns a Decimal without rounding to account precision. It raises `HistoricalFXUnavailable` when a required rate is missing, suspect, or too old. It uses historical storage exclusively.

- `HISTORICAL_FX_PUBLICATION_DELAY_MINUTES` sets a delay of 0–120 minutes after the Sydney midnight following the provider's session date. It defaults to 60. The cutoff is modeled. Historical publication timestamps and revision vintages are unavailable. Conversion respects both the stored and configured cutoff when the policy changes.
- `HISTORICAL_FX_HOLIDAYS` is a comma-separated list of confirmed provider closure dates in Sydney time. The default weekday schedule has no configured closures because the aggregate feed has no verified holiday list here. A missing weekday rate blocks conversion once due. During a weekend or a configured closure, the last expected session can be used within the age limit.
- `HISTORICAL_FX_MAX_AGE_HOURS` sets a ceiling of 1–96 elapsed hours, with a default of 96. Set a shorter `max_age` for a valuation when required. Coverage reports missing and suspect dates separately.

## Historical data readiness

Run the read-only command against the intended database. It reports expected, valid, missing and rejected OHLCV dates and the 30-session warm-up for the exact provider, instrument, MIC, currency and adjustment. For USD series it checks historical FX against the venue close of every expected candle, including warm-up. EUR requires no FX. Other currencies report unsupported FX coverage. The overall result evaluates readiness for a daily simulation valued in EUR at venue close.

```powershell
python manage.py historical_data_readiness --provider twelve --asset-class stock --symbol AAPL --exchange XNAS --currency USD --adjustment split --days 90 --end 2025-12-29
```

## Backtest a validated EUR OHLCV series

In a virtual account with the platform lab enabled, open **Historical simulations** and choose **Validated EUR OHLCV**. Enter one exact series using the provider, asset class, symbol, exchange, EUR currency and adjustment. Choose up to 365 calendar days. The completed sessions and all 30 expected warm-up sessions must have valid candles. The simulation queue stores a snapshot of the OHLCV data, venue times and source identity. The replay needs no provider or live quotes.

The OHLCV replay fills at the session open with existing fees, half the spread and slippage. It uses only prior closes whose modeled availability is strictly before the opening timestamp. A daily crypto close shares a timestamp with the next UTC open, so that close can first influence a later open. With exactly 30 warm-up candles this also means no crypto fill at the first opening in the requested window. Portfolio marks use session closes. The fill model and identity appear in the JSON export. The account-history replay and previously queued jobs retain their existing fill model, reports and walk-forward logic. Learning Lab, paper trading risk rules and strategies are unchanged.

For a controlled BTC/EUR run after approval, use the isolated validation database with the required schema and create a disposable virtual account there. Choose Binance, Crypto, BTC/EUR, BINANCE, EUR, and raw. Set 2026-08-26 through 2026-11-23. Queue manually, then process one queued job with `python manage.py run_simulations` using the same isolated configuration. Check the job's JSON export in the account.

## Isolated testing and credential screening

Run the PostgreSQL suite with `python manage.py test --settings=config.postgres_test_settings`.
Set `PGTESTHOST` to a loopback address and `PGTESTPORT` to the dedicated cluster's non-5432 port.
Set `PGTESTUSER`, `PGTESTPASSWORD` and a disposable `PGTESTDATABASE` starting with `test_`.
Django connects to that cluster's `postgres` maintenance database and creates the disposable database.
The standard settings reject the test command. An in-memory smoke suite is available with
`--settings=config.test_settings`. PostgreSQL-specific tests require the PostgreSQL configuration.

Run `python tools/scan_secrets.py` before committing. It checks tracked working-tree files for
common private keys, provider-token patterns and local dotenv files, and reports paths only.
Review new files before staging since the scan covers tracked files. This lightweight check
does not scan Git history or detect every credential format.

## Technical market screener

The original watchlist scanner links to **Technical screener** at `/research/technical/`.
Use the same `MARKET_SCANNER_UNIVERSE` (up to 100 instruments) and supply the exact series
for each technical row. For example:

```json
[{"group":"crypto","symbol":"BTC/EUR","exchange":"BINANCE","provider":"binance","currency":"EUR","adjustment":"raw"}]
```

Provider keys are `binance`, `twelve` and `alpaca`. Use a supported venue MIC for equities.
Legacy watchlist entries remain visible as unconfigured. Conflicting selections are unavailable.
The view reads stored bars in one batch and uses the existing cached exchange schedules. It never
requests quotes, FX, or backfills. Select a currency for price-denominated filters. Numeric
sorting groups by unit and keeps unknown values last. Volume uses shares for stocks and ETFs
and base units for crypto. Apply volume thresholds within a comparable universe.

All calculations use positive, valid OHLCV for completed venue sessions with observations available
at request time. Calculations use the contiguous run ending at the last available session within a
400-calendar-day lookback. A gap resets warm-up. If a later completed session is missing or rejected,
available values are marked stale and excluded from numeric filters. A missing value or unsupported
calendar produces an explicit status. Each metric carries value, unit, session date and provider.

| Metric | Definition and minimum history |
|---|---|
| Last close, day volume, day range | Latest candle. Range is `100 × (high − low) / close`. Missing volume stays unavailable. |
| SMA20/50/200 and distance to SMA20 | Mean of the last 20/50/200 closes. Distance is `100 × (close / SMA20 − 1)`. |
| 10-/20-session return | `100 × (close / close N sessions back − 1)`, requiring 11/21 closes. |
| Relative day volume | Latest completed volume divided by the mean of the previous 20 volumes. Requires all 21 observations and a positive mean. |
| Close volatility | Population standard deviation of the latest 20 daily log returns, multiplied by 100. Requires 21 closes. |
| RSI14 | Wilder averages of positive and negative changes. Seed each with the mean of the first 14 changes, then use weight 1/14. Requires 15 closes. A flat series gives 50. |
| MACD 12/26/9 | EMA12 minus EMA26. Seed each EMA with its initial SMA and use weight 2/(N+1). The line needs 26 closes. Its 9-period EMA signal and histogram need 34. |
| ATR14 | True range is `max(high, previous close) − min(low, previous close)`. Seed with the mean of the first 14 ranges, then use Wilder weight 1/14. Requires 15 candles. |
| 52-week high/low | Highest high and lowest low across all expected sessions in the 364 calendar days up to and including the metric's session date. Requires complete coverage. |

## Code map

| Path | Purpose |
|---|---|
| `config/settings.py` | Django, database, email, and security settings |
| `config/urls.py` | Application routes |
| `trading/models.py` | Accounts, instruments, orders, deposits, snapshots, and preferences |
| `trading/views.py` | User registration and trading pages |
| `trading/forms.py` | Account, instrument, deposit, and registration forms |
| `trading/engine.py` | Strategy signals, market data, simulated execution, and execution status |
| `trading/performance.py` | Capital snapshots, chart series, and paginated order history |
| `trading/strategy_lab.py` | Account comparison and historical strategy evaluation |
| `trading/profiles.py` | User profile, avatar, and account sidebar |
| `trading/templates/trading/` | Dashboard, account, profile, signal, and report templates |
| `trading/management/commands/run_bots.py` | One scheduled bot round |
| `trading/management/commands/send_reports.py` | Due email reports |
| `trading/migrations/` | Database schema changes tracked with the code |
| `trading/tests.py` | Django tests |

## Before sharing the app

The current setup is for local development. A shared virtual-only beta needs
a separate database, restricted access, HTTPS, working registration email,
and a market-data source that can be displayed to testers. A demo feed with
clearly labeled synthetic prices is the planned starting point for that beta.

Keep `env/`, `.venv/`, `.vs/`, local databases, database exports, passwords,
API keys, SMTP credentials, and Django secrets outside Git. Commit the source
files, templates, migrations, `requirements.txt`, `.gitignore`, and this
README. User records, profile photos, portfolios, and trades are stored in
PostgreSQL.

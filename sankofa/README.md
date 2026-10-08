# Sankofa

An Africa-focused commodities trading bot. It fuses four signal sources into one conviction per
instrument, filters them through a set of pan-African rules you control, sizes positions for a
high-risk profile, and runs unattended on a loop. It paper-trades by default.

```
copy-trade leaders ─┐
news / X sentiment ─┤                 ┌─ ethos rules ─┐
macro regime       ─┼─► combiner ────►│  (your values) │──► risk sizing ──► broker (paper / Alpaca / IBKR)
price trend        ─┘                 └────────────────┘        │
                                                                └──► journal + Telegram alerts
```

## Read this first: what is real and what is not

I built this to be honest about its own limits. Three things you asked for run into hard
external constraints, and the design works around them rather than pretending they do not exist.

**1. "African futures" are mostly not reachable by a retail bot.**
The African-exchange-listed contracts (JSE/SAFEX white maize, wheat, sunflower; the Ethiopia
Commodity Exchange) have no retail algorithmic API that I could verify. The universe therefore
uses Africa-linked instruments on Western exchanges: cocoa, coffee, cotton, sugar, gold,
platinum, palladium, copper, Brent, USD/ZAR, plus US-listed South African miners and the EZA
ETF. Every entry in `config/universe.yaml` carries an `africa_link` note explaining the link
and is marked `verified: false` until you confirm the symbol and contract spec with your broker.
The production-share figures in that file are approximate recollections, not cited statistics.

**2. There is no verified public API for copying Western social-trading leaders.**
eToro and similar platforms do not, as far as I can verify, expose a leader's live positions to
third-party code. So the bot does not scrape them. It ingests leader positions through two
generic channels (a JSON file inbox and an authenticated webhook) and you decide what feeds
them: a platform export, TradingView alerts, Collective2-style services, a friend's bot, or your
own scraper. Leader trust decays automatically when a leader's recorded calls lose money.

**3. The X/Twitter API is paid, and web scraping of X breaks constantly.**
The X collector uses the official v2 recent-search endpoint through `tweepy`, which requires a
bearer token on a paid tier. I do not have a verified current price; check developer.x.com.
Free, stable alternatives are built in: RSS feeds and Google News RSS search queries, which
give you a per-topic news "scraper" with no key. Run `sankofa once` on your machine to see
whether each feed returns items. From the sandbox where this was built, every external host
was blocked by a proxy, so the live feeds and the Yahoo Finance data source are **unverified
from here**. The unit tests cover the logic with synthetic data instead.

Also unverified from here: the Alpaca and Interactive Brokers adapters have not been run
against real accounts. Treat them as careful first drafts and exercise them on paper accounts.

## Pan-African ethos, as rules the code enforces

These live in `config/settings.yaml` under `ethos:` and are applied after signal fusion. They
are your stated values encoded as constraints, not claims about what is objectively right.

| Rule | Default | Effect |
|---|---|---|
| `african_linkage_required` | true | Only instruments with an `africa_link` are traded |
| `producer_aligned_long_boost` | 1.25 | Long conviction is scaled up where a rally benefits African producers (cocoa, coffee, gold, PGMs, copper, crude) |
| `no_short_african_equities` | true | Never short South African miners, Jumia, or the EZA ETF |
| `no_short_african_currencies` | true | Never go long USD/ZAR (a bet on rand weakness). Short USD/ZAR is allowed |
| `no_long_staples_on_food_shock_sentiment` | true | A famine or drought headline cannot by itself make the bot long maize or wheat; copy and trend legs still apply |
| `report_country_exposure` | true | Every cycle journals notional exposure by African country code |

## Risk profile (high, as requested, but bounded)

| Setting | Default |
|---|---|
| Risk per trade at stop | 4% of equity, scaled by conviction |
| Max single position | 30% of equity notional |
| Max gross exposure | 250% (2.5x) |
| Max open positions | 7 |
| Stop distance | 2.5 x ATR(14) |
| Daily loss: stop opening new positions | -8% |
| Peak-to-trough drawdown: flatten and halt | -30% |

The last two are kill switches. The bot refuses to start in live mode unless `mode: live` is
set in YAML **and** the environment variable `SANKOFA_LIVE_TRADING_ACK=yes` is present.
Futures contract multipliers in `sankofa/strategy/risk.py` are from memory; verify them against
your broker before live sizing.

## Signals

| Source | Where it comes from | Default weight |
|---|---|---|
| copytrade | File inbox and webhook, weighted by leader trust | 0.45 |
| sentiment | RSS + Google News RSS (+ optional X), VADER lexicon, with supply-shock direction logic for physical commodities | 0.20 |
| macro | USD strength (DXY), US rates (10y), risk appetite (EEM vs VIX), rand momentum | 0.20 |
| trend | 20/50 day moving-average spread, volatility-normalised | 0.15 |

Missing sources have their weight redistributed. `min_sources: 2` means a trend reading alone
never opens a trade. Sentiment for physical commodities is direction-corrected: a headline like
"shortage worsens as disease hits farms" is negative in tone but bullish for price, and the
scorer treats it that way. For equities it stays as plain sentiment.

## Install and run

```bash
cd sankofa
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,webhook]"          # add [alpaca], [ibkr], [x], [nlp] as needed
cp config/settings.example.yaml config/settings.yaml
cp .env.example .env
sankofa check-config                     # shows universe, ethos, risk and the live gate
sankofa once                             # one paper cycle against live feeds
sankofa run                              # the loop; leave this on overnight
sankofa status                           # equity, positions, last cycles
sankofa backtest --days 365              # trend+macro legs only, sanity check
pytest -q
```

Feeding a leader's positions:

```bash
sankofa push-leader leader_macro_1 '[{"symbol":"CC","direction":"long","size_pct":15}]'
# or, with the webhook running (sankofa run starts it):
curl -X POST http://127.0.0.1:8787/copytrade -H 'X-Sankofa-Secret: change-me' \
     -H 'Content-Type: application/json' -d @deploy/leader_example.json
```

Running while you sleep: `deploy/sankofa.service` (systemd on a VPS) or
`deploy/docker-compose.yml`. Both restart on failure, and the bot finishes its current cycle
on SIGTERM. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` and `alerts.telegram: true` to get
fills, stop-outs, kill-switch halts and a daily report on your phone.

## Backtest caveat

`sankofa backtest` replays only the trend and macro legs on daily data. Copy-trade and sentiment
have no history to replay, so it is not a backtest of the full strategy and its numbers should
not be quoted as expected performance.

## Layout

```
sankofa/
  config.py              settings + the live-trading gate
  universe.py            instruments, africa_link metadata, symbol aliasing
  data/market.py         Yahoo Finance (free, delayed) and a static source for tests
  signals/copytrade.py   leader book, file inbox, webhook, trust decay
  signals/news.py        RSS, Google News RSS, X v2 collectors
  signals/sentiment.py   VADER/FinBERT scoring, instrument tagging, supply-shock logic
  signals/macro.py       regime factors and class betas
  signals/trend.py       moving-average trend filter
  strategy/combiner.py   weighted fusion + ethos rules
  strategy/risk.py       sizing, exposure caps, kill switches
  execution/paper.py     persistent paper broker with stops
  execution/alpaca.py    US ETFs/ADRs (free paper endpoint)
  execution/ibkr.py      futures and FX via TWS/IB Gateway
  engine.py              one cycle; the overnight loop
  backtest.py, cli.py, journal.py, alerts.py
```

## Legal and practical notes

Automated trading of futures and FX may require registration or be restricted depending on
where you live and which broker you use. This code gives no investment advice and I have not
verified the regulatory position in any jurisdiction. Start on paper, run for weeks, read the
journal every morning, and only then consider small live size.

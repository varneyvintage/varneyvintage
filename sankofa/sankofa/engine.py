"""One trading cycle, and the overnight loop around it."""
from __future__ import annotations

import logging
import os
import signal as os_signal
import time
from collections import defaultdict
from datetime import UTC, datetime

from .alerts import Notifier
from .config import Settings
from .data.market import MarketData, YahooMarketData
from .execution.paper import PaperBroker
from .journal import Journal
from .signals.base import SignalSet
from .signals.copytrade import LeaderBook, start_webhook_server
from .signals.macro import MacroRegime
from .signals.news import GoogleNewsCollector, RSSCollector, XCollector
from .signals.sentiment import SentimentScorer
from .signals.trend import TrendSource
from .strategy.combiner import Combiner
from .strategy.risk import RiskManager
from .universe import load_universe

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings: Settings, market: MarketData | None = None, broker=None) -> None:
        self.s = settings
        self.universe = load_universe(settings.universe_file)
        self.market = market or YahooMarketData()
        self.journal = Journal(settings.state_dir)
        self.notify = Notifier(settings.alerts.get("telegram", False))
        self.broker = broker or self._make_broker()
        self.risk = RiskManager(settings.risk, settings.state_dir / "risk_state.json", self.broker.equity())
        self.combiner = Combiner(self.universe, settings.signals, settings.ethos)
        self.scorer = SentimentScorer(self.universe, settings.sentiment.get("engine", "vader"),
                                      settings.sentiment.get("min_mentions", 2))
        self.macro = MacroRegime(self.universe, self.market, settings.macro)
        self.trend = TrendSource(self.universe, self.market)
        ct = settings.copytrade
        self.leaders = LeaderBook(settings.state_dir, ct.get("leaders", []), ct.get("unknown_leader_weight", 0.3))
        self._webhook = None
        self._stop = False

    # ---- wiring --------------------------------------------------------------------
    def _make_broker(self):
        self.s.assert_live_allowed()
        name = self.s.broker_name()
        if not self.s.is_live or name == "paper":
            return PaperBroker(self.s.state_dir / "paper_account.json", self.s.starting_equity)
        if name == "alpaca":
            from .execution.alpaca import AlpacaBroker
            return AlpacaBroker(paper=(os.environ.get("ALPACA_PAPER", "true").lower() == "true"))
        if name == "ibkr":
            from .execution.ibkr import IBKRBroker
            return IBKRBroker()
        raise ValueError(f"unknown broker {name}")

    def start_sources(self) -> None:
        ct = self.s.copytrade
        if "webhook" in ct.get("sources", []):
            self._webhook = start_webhook_server(self.leaders, ct.get("webhook_host", "127.0.0.1"),
                                                 int(ct.get("webhook_port", 8787)), str(ct.get("webhook_secret", "")))

    # ---- collection ------------------------------------------------------------------
    def collect_text(self) -> list:
        sc = self.s.sentiment
        items = []
        hours = int(sc.get("lookback_hours", 24))
        if sc.get("rss_feeds"):
            items += RSSCollector(sc["rss_feeds"], hours).collect()
        if sc.get("google_news_queries"):
            items += GoogleNewsCollector(sc["google_news_queries"], hours).collect()
        xc = sc.get("x", {})
        token = os.environ.get("X_BEARER_TOKEN", "")
        if xc.get("enabled") and token:
            items += XCollector(token, xc.get("queries", []), xc.get("max_results_per_query", 50),
                                xc.get("boosted_accounts"), hours).collect()
        return items

    def collect_signals(self) -> SignalSet:
        sigs = SignalSet()
        ct = self.s.copytrade
        if "file" in ct.get("sources", []):
            self.leaders.ingest_inbox(self.s.root / ct.get("inbox_dir", "state/copytrade_inbox"))
        stale = self.s.signals.get("stale_after_minutes", {})
        sigs.add(*self.leaders.signals(self.universe, ct.get("require_universe_match", True),
                                       int(stale.get("copytrade", 720))))
        try:
            sigs.add(*self.scorer.score_items(self.collect_text()))
        except Exception as exc:
            log.exception("sentiment collection failed: %s", exc)
        try:
            sigs.add(*self.macro.signals())
        except Exception as exc:
            log.exception("macro failed: %s", exc)
        try:
            sigs.add(*self.trend.signals())
        except Exception as exc:
            log.exception("trend failed: %s", exc)
        return sigs

    # ---- one cycle -------------------------------------------------------------------
    def cycle(self) -> dict:
        started = datetime.now(UTC)
        if hasattr(self.market, "clear"):
            self.market.clear()
        # 1. prices & stops
        prices, bars = {}, {}
        for inst in self.universe:
            bar = self.market.last(inst.data_symbol)
            if bar:
                prices[inst.id], bars[inst.id] = bar.close, bar
        self.broker.mark(prices)
        if hasattr(self.broker, "check_stops"):
            for f in self.broker.check_stops(self.universe.get):
                self.journal.write("fill", **f.__dict__)
                self.notify.send(f"Stop hit: {f.instrument_id} {f.units:+.2f} @ {f.price:.4f}")
        equity = self.broker.equity()
        self.risk.update_equity(equity)

        # 2. kill switch
        if self.risk.state.halted:
            fills = self.broker.flatten_all(self.risk.state.halt_reason)
            for f in fills:
                self.journal.write("fill", **f.__dict__)
            self.notify.send(f"HALTED: {self.risk.state.halt_reason}. Flattened {len(fills)} positions.")
            self.journal.write("halt", reason=self.risk.state.halt_reason, equity=equity)
            return {"halted": True, "equity": equity}

        # 3. signals -> targets
        sigs = self.collect_signals()
        targets = self.combiner.combine(sigs)
        can_open, why = self.risk.can_open(equity)

        # 4. sizing
        desired: dict[str, tuple[float, float]] = {}
        stops: dict[str, float | None] = {}
        current = self.broker.positions()
        for t in targets:
            bar = bars.get(t.instrument.id)
            if not bar:
                continue
            units, stop = self.risk.desired_units(t, bar, equity)
            if units == 0.0:
                continue
            is_new_or_bigger = (t.instrument.id not in current
                                or abs(units) > abs(current[t.instrument.id].units)
                                or units * current[t.instrument.id].units < 0)
            if is_new_or_bigger and not can_open:
                continue
            uv = self.risk.unit_value(t.instrument.id, t.instrument.asset_class, bar.close)
            desired[t.instrument.id] = (units, uv)
            stops[t.instrument.id] = stop
        capped = self.risk.portfolio_caps(desired, equity)

        # 5. execute: close what is no longer wanted, then set the rest
        fills = []
        for inst_id in current:
            if inst_id not in capped:
                inst = self.universe.get(inst_id)
                if inst and inst_id in prices:
                    f = self.broker.trade_to(inst, 0.0, prices[inst_id], None, "signal faded")
                    if f:
                        fills.append(f)
        for inst_id, units in capped.items():
            inst = self.universe.get(inst_id)
            if inst.asset_class in ("futures", "etf", "equity"):
                units = float(int(units))
            if units == 0:
                continue
            t = next(x for x in targets if x.instrument.id == inst_id)
            reason = f"conv={t.conviction:+.2f} " + " ".join(f"{k}={v:+.2f}" for k, v in t.contributions.items())
            f = self.broker.trade_to(inst, units, prices[inst_id], stops.get(inst_id), reason)
            if f:
                fills.append(f)

        # 6. journal
        for f in fills:
            self.journal.write("fill", **f.__dict__)
        exposure = self.country_exposure()
        self.journal.write("cycle", equity=equity, can_open=can_open, why=why, n_signals=len(sigs.signals),
                           targets={t.instrument.id: {"conv": round(t.conviction, 3), "raw": round(t.raw, 3),
                                                      "contrib": {k: round(v, 3) for k, v in t.contributions.items()},
                                                      "ethos": t.ethos_notes} for t in targets},
                           positions={k: v.units for k, v in self.broker.positions().items()},
                           country_exposure=exposure,
                           seconds=(datetime.now(UTC) - started).total_seconds())
        if fills:
            self.notify.send("Fills:\n" + "\n".join(f"{f.instrument_id} {f.units:+.2f} @ {f.price:.4f} ({f.reason})" for f in fills))
        log.info("cycle done: equity=%.2f positions=%s", equity, {k: v.units for k, v in self.broker.positions().items()})
        return {"halted": False, "equity": equity, "fills": fills, "targets": targets, "signals": sigs}

    def country_exposure(self) -> dict[str, float]:
        """Notional attributed to African countries (split evenly across an instrument's countries)."""
        out: dict[str, float] = defaultdict(float)
        for inst_id, p in self.broker.positions().items():
            inst = self.universe.get(inst_id)
            if not inst or not inst.africa_link or not inst.africa_link.countries:
                continue
            px = getattr(self.broker, "_last", {}).get(inst_id, p.avg_price)
            notional = p.units * self.risk.unit_value(inst_id, inst.asset_class, px)
            share = notional / len(inst.africa_link.countries)
            for c in inst.africa_link.countries:
                out[c] += share
        return {k: round(v, 2) for k, v in sorted(out.items(), key=lambda kv: -abs(kv[1]))}

    # ---- loop --------------------------------------------------------------------------
    def run_forever(self) -> None:
        self.start_sources()
        interval = self.s.loop_interval_minutes * 60

        def _sig(*_):
            log.warning("shutdown requested; finishing current cycle")
            self._stop = True
        os_signal.signal(os_signal.SIGTERM, _sig)
        os_signal.signal(os_signal.SIGINT, _sig)

        self.notify.send(f"Sankofa started. mode={self.s.mode} broker={self.broker.name} equity={self.broker.equity():.2f}")
        last_report_day = None
        while not self._stop:
            try:
                self.cycle()
            except Exception as exc:
                log.exception("cycle crashed: %s", exc)
                self.notify.send(f"cycle crashed: {exc}")
            now = datetime.now(UTC)
            if now.hour == int(self.s.alerts.get("daily_report_hour_utc", 6)) and last_report_day != now.date():
                last_report_day = now.date()
                self.notify.send(self.daily_report())
            for _ in range(int(interval)):
                if self._stop:
                    break
                time.sleep(1)
        log.info("stopped")

    def daily_report(self) -> str:
        eq = self.broker.equity()
        st = self.risk.state
        pos = self.broker.positions()
        lines = [f"Sankofa daily report ({datetime.now(UTC):%Y-%m-%d})",
                 f"Equity {eq:,.2f} | peak {st.peak_equity:,.2f} | day start {st.day_start_equity:,.2f}",
                 f"Open positions: {len(pos)}"]
        for k, p in pos.items():
            lines.append(f"  {k}: {p.units:+.2f} @ {p.avg_price:.4f} stop {p.stop_price}")
        exp = self.country_exposure()
        if exp:
            lines.append("Country exposure: " + ", ".join(f"{k} {v:,.0f}" for k, v in list(exp.items())[:8]))
        return "\n".join(lines)

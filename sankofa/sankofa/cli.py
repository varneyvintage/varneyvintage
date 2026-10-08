"""Command line: sankofa run | once | status | backtest | push-leader | check-config"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC
from pathlib import Path

from .config import ConfigError, load_settings


def _logging(verbose: bool, state_dir: Path | None = None) -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if state_dir:
        handlers.append(logging.FileHandler(state_dir / "sankofa.log"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("yfinance").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sankofa", description="Africa-focused commodities trading bot")
    p.add_argument("-c", "--config", default=None, help="path to settings.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the loop forever (what you leave on overnight)")
    sub.add_parser("once", help="run a single cycle and exit")
    sub.add_parser("status", help="print equity, positions, last cycle")
    sub.add_parser("check-config", help="validate config and universe, show ethos rules")
    bt = sub.add_parser("backtest", help="sanity backtest of the trend+macro legs")
    bt.add_argument("--days", type=int, default=365)
    pl = sub.add_parser("push-leader", help="write a leader's positions into the copytrade inbox")
    pl.add_argument("leader")
    pl.add_argument("positions", help='JSON list, e.g. \'[{"symbol":"CC","direction":"long","size_pct":15}]\'')
    args = p.parse_args(argv)

    try:
        s = load_settings(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    _logging(args.verbose, s.state_dir)

    if args.cmd == "check-config":
        from .universe import load_universe
        uni = load_universe(s.universe_file)
        print(f"mode={s.mode} broker={s.broker_name()} equity={s.starting_equity} interval={s.loop_interval_minutes}m")
        print(f"universe: {len(uni)} instruments; unverified symbols: {sum(1 for i in uni if not i.verified)}")
        for i in uni:
            flag = "" if i.verified else "  [UNVERIFIED]"
            print(f"  {i.id:10s} {i.asset_class:8s} {i.data_symbol:10s} {','.join(i.africa_link.countries) if i.africa_link else '-'}{flag}")
        print("ethos:", json.dumps(s.ethos, indent=2))
        print("risk:", json.dumps(s.risk, indent=2))
        try:
            s.assert_live_allowed()
            print("live gate: OK" if s.is_live else "live gate: paper mode")
        except ConfigError as exc:
            print(f"live gate: {exc}")
        return 0

    if args.cmd == "push-leader":
        from datetime import datetime
        inbox = s.root / s.copytrade.get("inbox_dir", "state/copytrade_inbox")
        inbox.mkdir(parents=True, exist_ok=True)
        payload = {"leader": args.leader, "as_of": datetime.now(UTC).isoformat(),
                   "positions": json.loads(args.positions)}
        f = inbox / f"{args.leader}_{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
        f.write_text(json.dumps(payload, indent=2))
        print(f"wrote {f}")
        return 0

    if args.cmd == "backtest":
        from .backtest import run_backtest
        df = run_backtest(s, days=args.days)
        print(json.dumps({k: round(v, 2) for k, v in df.attrs["summary"].items()}, indent=2))
        out = s.state_dir / "backtest_equity.csv"
        df.to_csv(out)
        print(f"equity curve -> {out}")
        return 0

    from .engine import Engine
    eng = Engine(s)
    if args.cmd == "status":
        print(eng.daily_report())
        for rec in eng.journal.tail(5):
            print(json.dumps(rec, default=str)[:400])
        return 0
    if args.cmd == "once":
        res = eng.cycle()
        print(json.dumps({"halted": res["halted"], "equity": res["equity"],
                          "fills": [f.__dict__ for f in res.get("fills", [])],
                          "targets": {t.instrument.id: round(t.conviction, 3) for t in res.get("targets", [])}},
                         default=str, indent=2))
        return 0
    eng.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

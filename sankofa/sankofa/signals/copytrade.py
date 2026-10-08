"""Copy-trade ingestion.

Reality check: eToro and most 'social trading' brokers do not expose a public API for reading
a leader's live positions. (I could not verify one that does; if you have a platform with an
export or API, point an adapter at it.) So this module accepts leader positions through two
generic channels and leaves the "how do I get the leader's positions" question to you:

  1. File inbox  - drop a JSON file into state/copytrade_inbox/. Example:
        {"leader": "leader_macro_1", "as_of": "2026-10-08T01:00:00Z",
         "positions": [{"symbol": "CC", "direction": "long", "size_pct": 12.5, "note": "..."},
                       {"symbol": "XAUUSD", "direction": "short", "size_pct": 5}]}
     size_pct = % of the leader's portfolio in that position (0-100). Optional.
  2. Webhook  - POST the same JSON to http://<host>:<port>/copytrade with header
        X-Sankofa-Secret: <copytrade.webhook_secret>
     TradingView alerts, Zapier, a scraper you write, or a friend's bot can all push here.

Leader trust: each leader starts at its configured weight. After each cycle the bot records
what the leader was long/short and later marks the call against realised price moves; trust
multiplies by a factor in [0.5, 1.5] based on the leader's rolling hit rate (state/leader_scores.json).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

from ..universe import Universe
from .base import Signal, clip, utcnow

log = logging.getLogger(__name__)


def _parse_iso(s: str | None) -> datetime:
    if not s:
        return utcnow()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return utcnow()


class LeaderBook:
    """Latest known positions per leader, plus a persisted trust score per leader."""

    def __init__(self, state_dir: Path, leaders_cfg: list[dict], unknown_weight: float = 0.3) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.base_weight = {str(ld["id"]): float(ld.get("weight", 1.0)) for ld in leaders_cfg}
        self.unknown_weight = float(unknown_weight)
        self.positions: dict[str, dict] = {}  # leader -> payload
        self._scores_path = self.state_dir / "leader_scores.json"
        self.scores: dict[str, dict] = self._load_scores()
        self._lock = threading.Lock()

    # ---- trust -------------------------------------------------------------
    def _load_scores(self) -> dict:
        if self._scores_path.exists():
            try:
                return json.loads(self._scores_path.read_text())
            except json.JSONDecodeError:
                pass
        return {}

    def save_scores(self) -> None:
        self._scores_path.write_text(json.dumps(self.scores, indent=2, default=str))

    def trust(self, leader: str) -> float:
        base = self.base_weight.get(leader, self.unknown_weight)
        rec = self.scores.get(leader, {})
        wins, losses = rec.get("wins", 0), rec.get("losses", 0)
        n = wins + losses
        if n < 5:
            return base
        hit = wins / n
        # 50% hit rate -> x1.0 ; 70% -> x1.4 ; 30% -> x0.6   (bounded 0.5..1.5)
        return base * clip(1.0 + (hit - 0.5) * 2.0, 0.5, 1.5)

    def record_outcome(self, leader: str, won: bool) -> None:
        rec = self.scores.setdefault(leader, {"wins": 0, "losses": 0})
        rec["wins" if won else "losses"] += 1
        rec["updated"] = utcnow().isoformat()
        self.save_scores()

    # ---- ingestion -----------------------------------------------------------
    def ingest(self, payload: dict) -> bool:
        leader = str(payload.get("leader") or "").strip()
        if not leader or not isinstance(payload.get("positions"), list):
            log.warning("copytrade payload rejected (needs 'leader' and 'positions' list)")
            return False
        payload["as_of"] = _parse_iso(payload.get("as_of")).isoformat()
        with self._lock:
            prev = self.positions.get(leader)
            if prev and _parse_iso(prev["as_of"]) > _parse_iso(payload["as_of"]):
                return False  # stale
            self.positions[leader] = payload
        log.info("copytrade: %s -> %d positions", leader, len(payload["positions"]))
        return True

    def ingest_inbox(self, inbox: Path) -> int:
        inbox = Path(inbox)
        inbox.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in sorted(inbox.glob("*.json")):
            try:
                payload = json.loads(f.read_text())
                if self.ingest(payload):
                    n += 1
                f.rename(f.with_suffix(".json.done"))
            except Exception as exc:
                log.warning("bad inbox file %s: %s", f.name, exc)
                f.rename(f.with_suffix(".json.bad"))
        return n

    # ---- signals -------------------------------------------------------------
    def signals(self, universe: Universe, require_match: bool = True, stale_minutes: int = 720) -> list[Signal]:
        now = utcnow()
        agg: dict[str, list[tuple[float, float, str]]] = {}  # inst -> [(dir*size, trust, leader)]
        with self._lock:
            snapshot = dict(self.positions)
        for leader, payload in snapshot.items():
            as_of = _parse_iso(payload["as_of"])
            if (now - as_of).total_seconds() / 60 > stale_minutes:
                continue
            t = self.trust(leader)
            for pos in payload["positions"]:
                inst = universe.match_symbol(str(pos.get("symbol", "")))
                if inst is None:
                    if require_match:
                        continue
                    continue
                direction = str(pos.get("direction", "long")).lower()
                sign = 1.0 if direction in ("long", "buy") else -1.0
                size = float(pos.get("size_pct", 10.0))
                strength = clip(size / 20.0, 0.1, 1.0)  # 20%+ of a leader's book = full conviction
                agg.setdefault(inst.id, []).append((sign * strength, t, leader))
        out: list[Signal] = []
        for inst_id, rows in agg.items():
            tw = sum(t for _, t, _ in rows) or 1.0
            score = sum(s * t for s, t, _ in rows) / tw
            # more independent leaders agreeing -> more confidence
            conf = min(1.0, 0.5 + 0.25 * (len(rows) - 1)) * min(1.0, tw)
            out.append(Signal(source="copytrade", instrument_id=inst_id, score=score, confidence=conf, as_of=now,
                              evidence={"leaders": [name for _, _, name in rows], "n": len(rows)}))
        return out


def start_webhook_server(book: LeaderBook, host: str, port: int, secret: str) -> threading.Thread | None:
    """Tiny FastAPI receiver in a daemon thread. Returns None if fastapi is not installed."""
    try:
        import uvicorn
        from fastapi import FastAPI, Header, HTTPException, Request
    except ImportError:
        log.warning("fastapi/uvicorn not installed; webhook source disabled (pip install 'sankofa[webhook]')")
        return None

    app = FastAPI(title="sankofa copytrade webhook")

    @app.post("/copytrade")
    async def copytrade(req: Request, x_sankofa_secret: str = Header(default="")):
        if secret and x_sankofa_secret != secret:
            raise HTTPException(status_code=401, detail="bad secret")
        payload = await req.json()
        ok = book.ingest(payload)
        return {"accepted": ok}

    @app.get("/health")
    async def health():
        return {"ok": True, "leaders": list(book.positions)}

    def _run():
        uvicorn.run(app, host=host, port=port, log_level="warning")

    th = threading.Thread(target=_run, name="copytrade-webhook", daemon=True)
    th.start()
    log.info("copytrade webhook listening on http://%s:%d/copytrade", host, port)
    return th

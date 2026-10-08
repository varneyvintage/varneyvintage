"""Fuse per-source signals into one conviction per instrument, then apply the ethos rules."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..signals.base import Signal, SignalSet, clip
from ..universe import Instrument, Universe

log = logging.getLogger(__name__)


@dataclass
class Target:
    instrument: Instrument
    conviction: float                 # -1..1 after ethos adjustments
    raw: float                        # before ethos adjustments
    contributions: dict[str, float]   # source -> weighted contribution
    ethos_notes: list[str] = field(default_factory=list)
    blocked: bool = False


class Combiner:
    def __init__(self, universe: Universe, signal_cfg: dict, ethos_cfg: dict) -> None:
        self.universe = universe
        self.weights: dict[str, float] = dict(signal_cfg.get("weights", {}))
        self.stale: dict[str, float] = dict(signal_cfg.get("stale_after_minutes", {}))
        # require this many distinct fresh sources before an instrument gets a target
        self.min_sources = int(signal_cfg.get("min_sources", 1))
        self.ethos = ethos_cfg

    # ---- fusion ---------------------------------------------------------------
    def _fresh(self, sig: Signal) -> bool:
        limit = self.stale.get(sig.source)
        return limit is None or sig.age_minutes() <= float(limit)

    def combine(self, sigs: SignalSet) -> list[Target]:
        targets: list[Target] = []
        for inst in self.universe:
            per_source: dict[str, Signal] = {}
            for s in sigs.for_instrument(inst.id):
                if self._fresh(s) and (s.source not in per_source or s.as_of > per_source[s.source].as_of):
                    per_source[s.source] = s
            if len(per_source) < self.min_sources:
                continue
            # redistribute the weight of missing sources across the present ones
            present = {src: w for src, w in self.weights.items() if src in per_source}
            total = sum(present.values())
            if total <= 0:
                continue
            contributions = {src: (w / total) * per_source[src].score * per_source[src].confidence
                             for src, w in present.items()}
            raw = clip(sum(contributions.values()))
            t = Target(instrument=inst, conviction=raw, raw=raw, contributions=contributions)
            self.apply_ethos(t, per_source)
            targets.append(t)
        return targets

    # ---- ethos ----------------------------------------------------------------
    def apply_ethos(self, t: Target, per_source: dict[str, Signal]) -> None:
        e, inst = self.ethos, t.instrument
        if e.get("african_linkage_required", True) and inst.africa_link is None:
            t.blocked, t.conviction = True, 0.0
            t.ethos_notes.append("no africa_link -> excluded")
            return
        if t.conviction > 0 and inst.africa_link and inst.africa_link.producer_benefits_from_rally:
            boost = float(e.get("producer_aligned_long_boost", 1.0))
            if boost != 1.0:
                t.conviction = clip(t.conviction * boost)
                t.ethos_notes.append(f"producer-aligned long x{boost}")
        if t.conviction < 0 and e.get("no_short_african_equities", True) and inst.is_african_equity:
            t.conviction = 0.0
            t.ethos_notes.append("short African equity -> blocked")
        if t.conviction > 0 and e.get("no_short_african_currencies", True) and inst.is_african_fx:
            # LONG USDZAR profits from rand weakness
            t.conviction = 0.0
            t.ethos_notes.append("long USD vs African currency -> blocked")
        if (t.conviction > 0 and e.get("no_long_staples_on_food_shock_sentiment", True)
                and inst.id in set(e.get("staple_ids", []))):
            sent = per_source.get("sentiment")
            if sent and sent.evidence.get("food_shock_mentions", 0) > 0 and sent.score > 0:
                # strip the sentiment leg; keep copy/trend/macro legs
                t.conviction = clip(t.conviction - t.contributions.get("sentiment", 0.0))
                t.ethos_notes.append("food-shock sentiment leg removed on staple")

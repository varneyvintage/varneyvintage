import json

from sankofa.signals.copytrade import LeaderBook
from sankofa.universe import load_universe
from tests.conftest import ROOT


def test_inbox_ingest_and_signal(tmp_path):
    uni = load_universe(ROOT / "config" / "universe.yaml")
    book = LeaderBook(tmp_path, [{"id": "alpha", "weight": 1.0}], unknown_weight=0.3)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.json").write_text(json.dumps({
        "leader": "alpha", "positions": [
            {"symbol": "CC", "direction": "long", "size_pct": 20},
            {"symbol": "XAUUSD", "direction": "short", "size_pct": 10},
            {"symbol": "TSLA", "direction": "long", "size_pct": 50},   # not in universe -> ignored
        ]}))
    assert book.ingest_inbox(inbox) == 1
    assert (inbox / "a.json.done").exists()
    sigs = {s.instrument_id: s for s in book.signals(uni)}
    assert sigs["COCOA"].score == 1.0
    assert sigs["GOLD"].score == -0.5
    assert "TSLA" not in sigs


def test_unknown_leader_gets_low_trust_and_trust_decays(tmp_path):
    book = LeaderBook(tmp_path, [{"id": "alpha", "weight": 1.0}], unknown_weight=0.3)
    assert book.trust("stranger") == 0.3
    for _ in range(6):
        book.record_outcome("alpha", won=False)
    assert book.trust("alpha") == 0.5  # floor
    book2 = LeaderBook(tmp_path, [{"id": "alpha", "weight": 1.0}])
    assert book2.trust("alpha") == 0.5  # persisted


def test_stale_payload_is_rejected(tmp_path):
    book = LeaderBook(tmp_path, [])
    assert book.ingest({"leader": "x", "as_of": "2026-01-02T00:00:00Z", "positions": []})
    assert not book.ingest({"leader": "x", "as_of": "2026-01-01T00:00:00Z", "positions": []})

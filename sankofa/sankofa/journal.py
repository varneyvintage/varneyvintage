"""Append-only JSONL journal of every cycle, fill and decision. Your audit trail in the morning."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / "journal.jsonl"

    def write(self, kind: str, **data: Any) -> None:
        rec = {"ts": datetime.now(UTC).isoformat(), "kind": kind, **data}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(line) for line in lines if line.strip()]

"""Settings loading. YAML file + .env overrides; nothing else reads the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

try:  # optional: .env support
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


class ConfigError(RuntimeError):
    pass


@dataclass
class Settings:
    raw: dict[str, Any]
    root: Path

    # convenience accessors --------------------------------------------------
    @property
    def mode(self) -> str:
        return str(self.raw.get("mode", "paper")).lower()

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def state_dir(self) -> Path:
        p = self.root / self.raw.get("state_dir", "state")
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def universe_file(self) -> Path:
        return self.root / self.raw.get("universe_file", "config/universe.yaml")

    @property
    def risk(self) -> dict[str, Any]:
        return self.raw.get("risk", {})

    @property
    def ethos(self) -> dict[str, Any]:
        return self.raw.get("ethos", {})

    @property
    def signals(self) -> dict[str, Any]:
        return self.raw.get("signals", {})

    @property
    def copytrade(self) -> dict[str, Any]:
        return self.raw.get("copytrade", {})

    @property
    def sentiment(self) -> dict[str, Any]:
        return self.raw.get("sentiment", {})

    @property
    def macro(self) -> dict[str, Any]:
        return self.raw.get("macro", {})

    @property
    def alerts(self) -> dict[str, Any]:
        return self.raw.get("alerts", {})

    @property
    def loop_interval_minutes(self) -> int:
        return int(self.raw.get("loop_interval_minutes", 15))

    @property
    def starting_equity(self) -> float:
        return float(self.raw.get("starting_equity", 10_000))

    def env(self, key: str, default: str | None = None) -> str | None:
        return os.environ.get(key, default)

    def broker_name(self) -> str:
        return (self.env("SANKOFA_BROKER") or ("paper" if not self.is_live else "ibkr")).lower()

    def assert_live_allowed(self) -> None:
        """Live trading needs mode=live in YAML *and* the env acknowledgement."""
        if not self.is_live:
            return
        if (self.env("SANKOFA_LIVE_TRADING_ACK") or "").strip().lower() != "yes":
            raise ConfigError(
                "mode is 'live' but SANKOFA_LIVE_TRADING_ACK is not 'yes'. "
                "Refusing to start. Run in paper mode or set the acknowledgement deliberately."
            )


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.environ.get("SANKOFA_CONFIG") or "config/settings.yaml")
    if not path.exists():
        example = path.with_name("settings.example.yaml")
        if example.exists():
            raise ConfigError(f"{path} not found. Copy {example} to {path} and edit it.")
        raise ConfigError(f"Config file {path} not found.")
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    root = path.resolve().parent.parent  # config/settings.yaml -> project root
    return Settings(raw=raw, root=root)

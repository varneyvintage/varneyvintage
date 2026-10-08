import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway copy of config/ with its own state dir."""
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "universe.yaml", tmp_path / "config" / "universe.yaml")
    raw = yaml.safe_load((ROOT / "config" / "settings.example.yaml").read_text())
    raw["sentiment"]["rss_feeds"] = []
    raw["sentiment"]["google_news_queries"] = []
    raw["copytrade"]["sources"] = ["file"]
    raw["sentiment"]["reddit"]["enabled"] = False
    (tmp_path / "config" / "settings.yaml").write_text(yaml.safe_dump(raw))
    (tmp_path / "state").mkdir()
    return tmp_path


def synthetic_frame(start: float, drift: float, n: int = 200, vol: float = 0.01, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = start * np.cumprod(1 + rets)
    high = close * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = close * (1 - np.abs(rng.normal(0, vol / 2, n)))
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": 1000}, index=idx)

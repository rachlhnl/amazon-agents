"""Daily market tracker: public data on our brand, a head-to-head rival and peers.

  collect()  read every brand in market/brands.toml into a dated snapshot
  analyze    trends, opportunities and plain-language reads (analyze.py)
  render     the tabbed dashboard (render.py)

Snapshots are kept as market/history/YYYY-MM-DD.json.gz; trends compare them.
"""

from __future__ import annotations

import gzip
import json
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import sources

ROOT = Path("market")


def load_config(root: Path = ROOT) -> dict[str, dict[str, Any]]:
    return tomllib.loads((root / "brands.toml").read_text())["brand"]


def collect(root: Path = ROOT, log=print) -> dict[str, Any]:
    brands: dict[str, Any] = {}
    for key, cfg in load_config(root).items():
        rec: dict[str, Any] = {"name": cfg["name"], "role": cfg["role"], "errors": []}
        if store := cfg.get("store"):
            try:
                rec["store"] = sources.shopify_catalog(store, reviews=cfg.get("reviews", False))
                log(f"{cfg['name']}: {len(rec['store']['products'])} store products")
            except Exception as e:  # noqa: BLE001 - one broken store shouldn't stop the run
                rec["errors"].append(f"store: {type(e).__name__}: {e}"[:300])
                log(f"{cfg['name']}: store failed ({e})")
        if asins := cfg.get("amazon"):
            rec["amazon"] = sources.amazon_items(asins)
            ok = sum(1 for a in rec["amazon"] if "error" not in a)
            log(f"{cfg['name']}: {ok}/{len(asins)} Amazon listings read")
        brands[key] = rec
    return {
        "date": date.today().isoformat(),
        "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "brands": brands,
    }


def save(snap: dict[str, Any], root: Path = ROOT) -> Path:
    hist = root / "history"
    hist.mkdir(parents=True, exist_ok=True)
    path = hist / f"{snap['date']}.json.gz"
    # mtime=0 keeps the gzip bytes stable, so an unchanged day makes no git diff.
    with gzip.GzipFile(path, "wb", mtime=0) as f:
        f.write(json.dumps(snap, separators=(",", ":"), sort_keys=True).encode())
    return path


def history(root: Path = ROOT) -> list[dict[str, Any]]:
    """Every saved snapshot, oldest first."""
    out = []
    for p in sorted((root / "history").glob("*.json.gz")):
        with gzip.open(p) as f:
            out.append(json.load(f))
    return out

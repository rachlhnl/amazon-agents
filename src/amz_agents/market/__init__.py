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
        if shop := cfg.get("judgeme_shop"):
            try:
                n = collect_reviews(key, shop, rec.get("store", {}).get("products", []), root)
                log(f"{cfg['name']}: {n} new store reviews")
            except Exception as e:  # noqa: BLE001 - reviews are extra; keep the snapshot
                rec["errors"].append(f"reviews: {type(e).__name__}: {e}"[:300])
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


def reviews(key: str, root: Path = ROOT) -> list[dict[str, Any]]:
    """Every store review collected so far for a brand (kept across days)."""
    path = root / "reviews" / f"{key}.json.gz"
    if not path.exists():
        return []
    with gzip.open(path) as f:
        return json.load(f)


def collect_reviews(key: str, shop: str, products: list[dict], root: Path = ROOT) -> int:
    have = reviews(key, root)
    known = {r["id"] for r in have}
    new = []
    with sources._client() as http:
        for p in products:
            new += sources.judgeme_reviews(http, shop, p["id"], known)
    if new:
        path = root / "reviews" / f"{key}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = sorted(have + new, key=lambda r: (r["date"], r["id"]))
        with gzip.GzipFile(path, "wb", mtime=0) as f:
            f.write(json.dumps(merged, separators=(",", ":")).encode())
    return len(new)


def save(snap: dict[str, Any], root: Path = ROOT) -> Path:
    hist = root / "history"
    hist.mkdir(parents=True, exist_ok=True)
    path = hist / f"{snap['date']}.json.gz"
    if path.exists():
        # A second run on the same day mustn't replace good Amazon reads with blocked ones.
        with gzip.open(path) as f:
            earlier = json.load(f)
        for key, b in snap["brands"].items():
            good = {a["asin"]: a for a in earlier["brands"].get(key, {}).get("amazon", [])
                    if "error" not in a}
            if "amazon" in b:
                b["amazon"] = [good.get(a["asin"], a) if "error" in a else a for a in b["amazon"]]
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

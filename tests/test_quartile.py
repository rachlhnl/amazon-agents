import csv

import pytest

from amz_agents import quartile
from amz_agents.dashboard import collect, render
from amz_agents.data.warehouse import Warehouse

NOTE = "Applied filters:\nASIN is not SB or SD\nDate 8/20/2026 - 10/20/2026"
HEAD = ["ParentASIN", "ASIN", "Total Sales", "TACoS", "Ad Sales", "Ad Spend", "ACoS", "Name"]
SET = "Dagacci Medical Uniform Scrub Set Unisex Top and Pant"
ROWS = [
    ["P1", "A1", 90000, 0.05, 30000, 3000, 0.10, f"{SET}, Navy, Medium"],
    ["P1", "A2", 60000, 0.04, 20000, 2400, 0.12, f"{SET}, Black, Large,"],
    ["P1", "A3", 0, None, 0, 5, None, f"{SET}, Red, XXL"],
    ["P2", "B1", 8000, 0.25, 6000, 2000, 0.33, "Dagacci Women's Jogger Set (Medium, Navy Blue)"],
    ["P2", "B2", 4000, 0.10, 3000, 400, 0.13, "Dagacci Women's Jogger Set (Small, Hot Pink)"],
    ["P3", "C1", 5000, 0.01, 2000, 60, 0.03, "Dagacci Scrub Cap Pewter Grey XL"],
]


def _csv(tmp_path, rows=ROWS, note=NOTE):
    path = tmp_path / "data.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([note])
        w.writerow([])
        w.writerow(HEAD)
        w.writerows(rows)
        w.writerow(["Total", "", 167000, "", 61000, 7865, "", ""])
    return path


@pytest.fixture
def wh():
    return Warehouse(":memory:")


def test_reads_dates_ad_types_and_rows(tmp_path):
    ex = quartile.read_export(_csv(tmp_path))
    assert (ex["start"], ex["end"]) == ("2026-08-20", "2026-10-20")
    assert ex["ad_types"] == "Sponsored Products"
    assert len(ex["rows"]) == 6  # the Total row is skipped
    assert ex["rows"][0] == {"parent_asin": "P1", "asin": "A1", "total_sales": 90000.0,
                             "ad_sales": 30000.0, "ad_spend": 3000.0,
                             "name": f"{SET}, Navy, Medium"}


def test_load_needs_dates_when_the_file_has_none(tmp_path, wh):
    path = _csv(tmp_path, note="exported")
    with pytest.raises(ValueError):
        quartile.load(wh, path)
    got = quartile.load(wh, path, "2026-09-01", "2026-09-30")
    assert got["ad_types"] == "all ad types" and got["rows"] == 6


def test_reloading_a_period_replaces_it(tmp_path, wh):
    quartile.load(wh, _csv(tmp_path))
    quartile.load(wh, _csv(tmp_path, rows=ROWS[:2]))
    assert wh.query("SELECT COUNT(*) AS n FROM ad_products")[0]["n"] == 2


def test_variant_sizes_and_colors():
    known = quartile.colors_known([r[7] for r in ROWS])
    assert quartile.variant(f"{SET}, Navy, Medium", known) == ("M", "navy")
    assert quartile.variant(f"{SET}, Black, Large,", known) == ("L", "black")
    assert quartile.variant("Dagacci Women's Jogger Set (Medium, Navy Blue)", known) == ("M", "navy")
    assert quartile.variant("Dagacci Scrub Cap Pewter Grey XL", {"pewter gray"}) == ("XL", "pewter gray")


def test_view_totals_styles_and_findings(tmp_path, wh):
    quartile.load(wh, _csv(tmp_path))
    v = quartile.view(wh)
    t = v["totals"]
    assert v["days"] == 62 and t["sales"] == 167000 and t["spend"] == 7865
    assert t["acos"] == pytest.approx(7865 / 61000)
    assert t["selling"] == 5 and t["asins"] == 6
    assert v["styles"][0]["parent"] == "P1" and v["styles"][0]["name"] == "Scrub Set Unisex Top and Pant"
    assert v["styles"][1]["name"] == "Women's Jogger Set"
    sizes = {s["size"]: s["sales"] for s in v["sizes"]}
    assert sizes["M"] == 98000 and sizes["L"] == 60000
    titles = " | ".join(f["title"] for f in v["findings"])
    assert "90% of Amazon sales are one style" in titles
    assert "1 product cost much more per ad sale" in titles  # B1: 33% ACoS, $2,000 spend
    costly = next(f for f in v["findings"] if f["title"].startswith("1 product cost"))
    assert costly["items"] == ["B1"] and costly["gain"] > 0
    assert any(f["kind"] == "catalog" for f in v["findings"])  # A3 sold nothing
    assert set(v["asins"]) >= {"A1", "B1"}


def test_tracker_shows_the_ads_tab(tmp_path, wh):
    assert collect(wh, include_market=False)["ads"] is None
    quartile.load(wh, _csv(tmp_path))
    data = collect(wh, include_market=False)
    assert data["ads"]["totals"]["sales"] == 167000
    page = render(data)
    assert 'id="tab-ads"' in page and "Amazon ads" in page

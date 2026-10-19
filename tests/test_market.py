import base64
import copy
import json

from amz_agents.market import analyze, render, sentiment, sources

PAGE = """
<span id="productTitle" class="a-size-medium">  COZY SCRUBS Women&#39;s Set  </span>
<a id="bylineInfo" href="/stores/x">Visit the COZYFIT Store</a>
"parentAsin":"B0PARENT01" "priceAmount":19.99
<span id="acrPopover" class="x" title="4.2 out of 5 stars"></span>
<span id="acrCustomerReviewText" aria-label="1,726 Reviews">(1,726)</span>
<span>2K+ bought in past month</span>
<th>Best Sellers Rank</th><td><span>#12,476 in Clothing, Shoes &amp; Jewelry (See Top 100)</span>
<span>#21 in <a>Women's Medical Scrub Sets</a></span></td></table>
"""


def test_parse_amazon_page():
    x = sources.parse_amazon_page("B0TEST0001", PAGE)
    assert x["title"] == "COZY SCRUBS Women's Set"
    assert x["parent"] == "B0PARENT01"
    assert x["price"] == 19.99 and x["rating"] == 4.2 and x["reviews"] == 1726
    assert x["bought_past_month"] == 2000
    assert x["ranks"]["Women's Medical Scrub Sets"] == 21
    assert analyze.scrub_rank(x) == ("Women's Medical Scrub Sets", 21)


def test_parse_amazon_page_blocked():
    assert sources.parse_amazon_page("B0X", "<form action='/errors/validateCaptcha'>")["error"] == "captcha"
    assert "error" in sources.parse_amazon_page("B0X", "<html></html>")


def test_product_line_uses_whole_words():
    assert sources.product_line("The Mari Scrub Set - Burgundy") == "Scrub sets"
    assert sources.product_line("Women's Inset Pocket Top") == "Tops"
    assert sources.product_line("High Waisted Flares Eggplant") == "Pants"


def prod(pid, title, price, colors=(), **kw):
    return {"id": pid, "title": title, "url": f"https://x/{pid}", "line": sources.product_line(title),
            "vendor": "x", "price": price, "price_max": price, "compare_at": kw.get("compare_at"),
            "variants": kw.get("variants", 10), "in_stock": kw.get("in_stock", 10),
            "colors": max(1, len(colors)), "color_names": list(colors),
            "created": kw.get("created", "2024-01-01"), "rating": kw.get("rating"),
            "reviews": kw.get("reviews", 0)}


def test_styles_group_colors_listed_separately():
    ps = [prod(1, "Mari Scrub Set - Burgundy", 40, ["Burgundy"]),
          prod(2, "Mari Scrub Set - Black", 40, ["Black"]),
          prod(3, "Fierce Jogger | Lite | Black", 30, ["Black"]),
          prod(4, "Cargos Eggplant", 25), prod(5, "Joggers Eggplant", 25),
          prod(6, "Flares Eggplant", 25), prod(7, "Cargos Plum", 25)]
    st = analyze.styles(ps)
    assert st["mari scrub set"] == {"burgundy", "black"}
    assert "fierce jogger | lite" in st
    assert "cargos" in st and "eggplant" in st["cargos"]


def store(products):
    return {"source": "https://x", "products": products}


def snap(day, dag_products, amazon):
    return {"date": day, "collected_at": day + "T12:00:00+00:00", "brands": {
        "dagacci": {"name": "Dagacci", "role": "self", "errors": [], "store": store(dag_products),
                    "amazon": amazon},
        "cozyfit": {"name": "Cozyfit", "role": "rival", "errors": [], "amazon": [
            {"asin": "B0C1", "title": "COZYFIT Scrubs for Women Set", "price": 19.99, "rating": 4.3,
             "reviews": 5000, "ranks": {"Women's Medical Scrub Sets": 3}}]},
        "peer": {"name": "Peer", "role": "peer", "errors": [], "store": store(
            [prod(100 + i, f"Style {i} Scrub Set", 60, ["Navy", "Black"], created="2026-10-01")
             for i in range(6)])},
    }}


CONFIG = {"dagacci": {"name": "Dagacci", "role": "self", "store": "www.dagacci.com"},
          "cozyfit": {"name": "Cozyfit", "role": "rival",
                      "notes": [{"title": "Watch this", "text": "A note."}]},
          "peer": {"name": "Peer", "role": "peer", "store": "peer.example"}}


def test_build_trends_changes_and_carry_forward():
    ps = [prod(i, f"Set {i} Scrub Set", 25 + i, ["Navy"] * 1, rating=4.4, reviews=60)
          for i in range(5)]
    ps.append(prod(9, "Lab Coat", 20, in_stock=0, rating=4.5, reviews=30))
    day1 = snap("2026-10-12", ps, [{"asin": "B0D1", "title": "Dagacci Set", "price": 24.99,
                                    "rating": 4.0, "reviews": 1000,
                                    "ranks": {"Women's Medical Scrub Sets": 10}}])
    ps2 = copy.deepcopy(ps)
    ps2[0]["price"] = 29.99
    ps2[1]["reviews"] = 75
    ps2[5]["in_stock"] = 4
    day2 = snap("2026-10-19", ps2, [{"asin": "B0D1", "error": "captcha"}])

    out = analyze.build([day1, day2], CONFIG)
    secs = {s["id"]: s for s in out["sections"]}
    assert list(secs) == ["sentiment", "catalog", "rival", "market", "trends"]
    charts = [c for s in out["sections"] for c in s["charts"]]

    kinds = {c["kind"] for c in secs["trends"]["changes"]}
    assert {"Price", "Reviews", "Restocked"} <= kinds
    # Blocked Amazon read: last good values carried forward, flagged as stale.
    table = next(c for c in secs["catalog"]["charts"] if c["type"] == "table")
    assert table["rows"][0][2] == 24.99 and "last good" in table["rows"][0][-1]
    # Every recommendation says what we see, why, what to do, and when.
    assert out["recs"] and all(r["see"] and r["why"] and r["do"] and r["when_label"]
                               for r in out["recs"])
    assert [r["when"] for r in out["recs"]] == sorted(r["when"] for r in out["recs"])
    assert any(r["area"] == "Data" for r in out["recs"])
    assert any(r["title"] == "Watch this" for r in out["recs"])
    # Every chart has a plain-language read and a width, and trend lines have two points.
    assert all(c.get("read") for c in charts if c["type"] not in ("table", "note"))
    assert all(c["span"] in (4, 5, 6, 7, 8, 12) for c in charts)
    reviews = next(c for c in charts if c["title"] == "Reviews on dagacci.com")
    assert len(reviews["series"][0]["points"]) == 2 and "rose" in reviews["read"]

    html = render.render(out)
    assert "/*ANALYSIS*/" not in html and "Dagacci" in html


def test_delta_picks_point_about_a_week_back():
    pts = [["2026-10-01", 1.0], ["2026-10-11", 2.0], ["2026-10-12", 3.0], ["2026-10-19", 5.0]]
    assert analyze.delta(pts, 7) == (5.0, 3.0, 7)
    assert analyze.delta(pts[:1], 7) is None


def test_customers_say_topics():
    blob = {"k-data": {"sentiments": [
        {"label": "positive", "aspects": [{"label": "quality", "mentions": 100, "sentimentMentions": 90,
                                           "summary": "Customers like the quality.", "snippets": [
            {"review": {"url": "/r/1"}, "text": {"fragments": [
                {"semanticContent": {"content": {"text": "Great quality"}}}, {"text": ", soft."}]}}]}]},
        {"label": "mixed", "aspects": [{"label": "size", "mentions": 50, "sentimentMentions": 20,
                                        "summary": "Mixed on size.", "snippets": []}]}]}}
    page = "k-injected k+b64 " + base64.b64encode(json.dumps(blob).encode()).decode()
    x = sources.amazon_customers_say(page)
    assert x["aspects"][0]["snippets"][0]["text"] == "Great quality, soft."
    t = sentiment.amazon_topics([{"asin": "A", **x}])
    assert t["quality"]["pos"] == 90 and t["quality"]["mid"] == 10
    assert t["size"]["mid"] == 50 and t["size"]["worst"]["verdict"] == "mixed"


def test_store_sentiment_pinpoints_fit_problem():
    products = [{"id": 1, "title": "Jogger Set", "url": "https://x/1"}]
    reviews = [{"id": str(i), "product_id": 1, "score": 5, "date": "2026-05-01", "title": "",
                "body": "So comfortable and soft"} for i in range(8)]
    reviews += [{"id": f"n{i}", "product_id": 1, "score": 1, "date": "2026-06-01", "title": "",
                 "body": "Way too small. It runs small, size up."} for i in range(4)]
    st = sentiment.store_sentiment(reviews, products, "2026-10-19")
    pin = st["pinpoints"][0]
    assert pin["topic"] == "Fit & sizing" and pin["detail"] == "runs small"
    assert pin["share"] == 1.0 and "too small" in pin["quote"].lower()
    assert st["topics"]["Stretch & comfort"]["pos"] == 8

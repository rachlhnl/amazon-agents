import httpx

from amz_agents.market import shopper

PAGE = """<html><head><script>var x = "afterpay";</script></head><body>
<h1>Classic V-Neck Scrub Set</h1><a>Size Guide</a><p>72% polyester, 21% rayon, 7% spandex.
Machine wash cold. Model is 5'8" wearing size S. 6-pocket top. Petite and tall lengths.</p>
<div class="jdgm-widget"></div><p>Free shipping on orders over $50.</p></body></html>"""
POLICY = ('<div class="shopify-policy__body"><p>Free exchanges within 30 days of delivery.</p>'
          "</div></div>")


def _http(page=PAGE):
    def handle(req):
        if req.url.path.endswith(".json"):
            return httpx.Response(200, json={"product": {"images": [1, 2, 3], "options": [
                {"name": "Size", "values": ["XS", "S", "M", "L", "XL"]}], "body_html": ""}})
        if "/policies/" in req.url.path:
            return httpx.Response(200, text=POLICY)
        return httpx.Response(200, text=page)
    return httpx.Client(transport=httpx.MockTransport(handle))


PRODUCTS = [{"title": "Classic V-Neck Scrub Set", "url": "https://x.test/products/set",
             "line": "Scrub sets", "in_stock": 10, "price": 30.0, "variants": 20, "colors": 4,
             "vendor": "X", "reviews": 12, "rating": 4.5},
            {"title": "Scrunchies Set", "url": "https://x.test/products/s", "line": "Scrub sets",
             "in_stock": 5, "price": 9.0, "variants": 1, "colors": 1, "vendor": "X"}]


def test_visit_finds_what_a_shopper_looks_for(monkeypatch):
    monkeypatch.setattr(shopper, "DELAY", 0)
    v = shopper.visit_store(_http(), "X", "x.test", PRODUCTS)
    assert v["product"]["title"] == "Classic V-Neck Scrub Set"  # not the scrunchies
    f = v["found"]
    for key in ("size_guide", "fabric", "care", "model", "pockets", "lengths", "reviews",
                "free_returns", "return_window", "free_shipping", "pay_later"):
        assert f[key], key
    assert not f["help"] and not f["fit_tool"]
    assert v["return_days"] == 30 and v["free_ship_over"] == 50 and len(v["product"]["sizes"]) == 5


def test_scores_reviews_and_verdict(monkeypatch):
    monkeypatch.setattr(shopper, "DELAY", 0)
    good = shopper.visit_store(_http(), "Good", "x.test", PRODUCTS)
    bare = shopper.visit_store(_http("<html><body>Scrub set</body></html>" + "x" * 3_200_000),
                               "Us", "x.test", PRODUCTS)
    for v, price in ((good, 60.0), (bare, 25.0)):
        v["outfit"] = price
        v["scores"] = shopper.score(v, 40.0)
        v["overall"] = shopper.overall(v["scores"])
        v["review"] = shopper.narrate(v)
    assert good["overall"] > bare["overall"]
    assert shopper._value(25, 40) > shopper._value(40, 40) > shopper._value(60, 40)
    assert any("heavy" in d for d in bare["review"]["dislikes"])
    assert any("Size guide" in x for x in good["review"]["likes"])
    v = shopper.verdict([good, bare], "Us")
    assert v["choice"] == "Good" and "cheaper buy" in v["why"]
    gaps = shopper.findings({"self": "Us", "visits": [good, bare]})
    titles = [g["title"] for g in gaps]
    assert titles[0].startswith("dagacci.com product pages are heavy")
    assert any("size guide" in t for t in titles) and all(g["area"] == "Website" for g in gaps)

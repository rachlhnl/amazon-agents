from amz_agents import seller_reports
from amz_agents.data.warehouse import Warehouse

ORDERS = (
    "amazon-order-id\tpurchase-date\torder-status\tsales-channel\tproduct-name\tsku\tasin\tquantity\titem-price\n"
    "111\t2026-10-17T10:00:00+00:00\tShipped\tAmazon.com\tScrub Set Navy M\tSET-NAVY-M\tB001\t2\t49.98\n"
    "112\t2026-10-17T12:00:00+00:00\tPending\tAmazon.com\tScrub Set Navy M\tSET-NAVY-M\tB001\t1\t\n"
    "113\t2026-10-18T09:00:00+00:00\tCancelled\tAmazon.com\tScrub Set Navy M\tSET-NAVY-M\tB001\t1\t24.99\n"
    "114\t2026-10-18T09:30:00+00:00\tShipped\tAmazon.com\tJogger Black S\tJOG-BLK-S\tB002\t1\t29.99\n"
    "115\t2026-10-18T10:00:00+00:00\tShipped\tAmazon.com.mx\tJogger Black S\tJOG-BLK-S\tB002\t1\t599.00\n"
    "116\t2026-10-18T11:00:00+00:00\tShipped\tNon-Amazon\tJogger Black S\tJOG-BLK-S\tB002\t1\t\n"
)
TRAFFIC = (
    '"(Parent) ASIN","(Child) ASIN","Title","Sessions – Total","Page Views – Total",'
    '"Featured Offer (Buy Box) Percentage","Units Ordered","Ordered Product Sales"\n'
    '"P1","B001","Scrub Set","1,200","1,500","92.5%","130","$3,248.70"\n'
    '"P1","B999","Unknown","10","12","100%","0","$0.00"\n'
)
INVENTORY = ("sku\tfnsku\tasin\tproduct-name\tafn-fulfillable-quantity\t"
             "afn-inbound-working-quantity\tafn-inbound-shipped-quantity\t"
             "afn-inbound-receiving-quantity\n"
             "SET-NAVY-M\tX1\tB001\tScrub Set Navy M\t340\t0\t100\t20\n")


def test_loads_each_report_in_the_right_order(tmp_path):
    wh = Warehouse(":memory:")
    wh.insert_many("metrics_daily", [{"date": "2026-10-17", "sku": "SET-NAVY-M", "units": 0,
                                      "revenue": 0, "ad_spend": 5.0, "ad_sales": 20.0}])
    files = []
    for name, text in [("traffic.csv", TRAFFIC), ("orders.txt", ORDERS), ("fba.txt", INVENTORY)]:
        (tmp_path / name).write_text(text, encoding="utf-8")
        files.append(tmp_path / name)
    out = {r["report"]: r for r in seller_reports.load(wh, files)}
    assert out["orders"]["units"] == 4 and out["orders"]["revenue"] == 79.97  # US only
    assert out["orders"]["skipped"] == {"Amazon.com.mx": 1, "Non-Amazon": 1}
    assert out["traffic"] == {"file": "traffic.csv", "report": "traffic", "rows": 1, "unmatched": 1,
                              "note": out["traffic"]["note"]}
    m = {(r["date"], r["sku"]): r for r in wh.query("SELECT * FROM metrics_daily")}
    assert m[("2026-10-17", "SET-NAVY-M")]["units"] == 3  # pending counts, no price yet
    assert m[("2026-10-17", "SET-NAVY-M")]["ad_spend"] == 5.0  # kept
    assert ("2026-10-18", "SET-NAVY-M") not in m  # cancelled
    p = {r["sku"]: r for r in wh.query("SELECT * FROM products")}
    assert p["SET-NAVY-M"]["asin"] == "B001" and p["SET-NAVY-M"]["units_on_hand"] == 340
    assert p["SET-NAVY-M"]["units_inbound"] == 120
    t = wh.query("SELECT * FROM asin_traffic")[0]
    assert t["sku"] == "SET-NAVY-M" and t["sessions_30d"] == 1200 and t["buy_box_pct"] == 0.925


def test_unknown_files_are_reported_not_loaded(tmp_path):
    (tmp_path / "x.csv").write_text("a,b\n1,2\n")
    assert seller_reports.load(Warehouse(":memory:"), [tmp_path / "x.csv"])[0]["error"]

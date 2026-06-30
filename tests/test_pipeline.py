"""Offline end-to-end test: simulates 6 daily runs with synthetic catalogs and
asserts sold detection, relist handling, price changes, days-to-sell, metrics.

Run:  BAGINTEL_DB=/tmp/test.db python tests/test_pipeline.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("BAGINTEL_DB", "/tmp/bagintel_test.db")
FIXDIR = tempfile.mkdtemp()


def product(pid, title, price, available=True, vendor="Hermes", tags=None,
            body="", img="x.jpg"):
    return {
        "id": pid, "title": title, "handle": f"h{pid}",
        "body_html": body, "published_at": "2026-06-01T08:00:00+08:00",
        "created_at": "2026-06-01T08:00:00+08:00", "vendor": vendor,
        "product_type": "Bags", "tags": tags or [],
        "variants": [{"price": str(price), "available": available}],
        "images": [{"src": f"https://cdn/{img}"}],
    }


BASE = {
    "pursemaison": [
        product(1, "Birkin 25 in Gold Togo Leather GHW", 1500000),
        product(2, "Kelly 28 in Etoupe Epsom PHW", 1300000),
        product(3, "Chanel Classic Flap Medium Black Caviar GHW", 600000, vendor="Chanel"),
        product(4, "Birkin 30 Rouge Casaque placeholder", 0),         # ₱0 excluded
        product(5, "Constance 18 Bleu Jean Epsom GHW", 900000),
    ],
    "orangebox": [
        product(11, "Brand New Hermes Birkin 25 Noir Togo PHW", 1600000, vendor="Store Inventory"),
        product(12, "Chanel Jumbo Black Lambskin GHW", 550000, vendor="Store Inventory"),
        product(13, "LV Neverfull MM Damier", 90000, vendor="Store Inventory"),
    ],
    "baghub": [product(21, "Hermes Mini Kelly Gold Epsom GHW", 1900000,
                       vendor="The Bag Hub", tags=["Hermes"])],
    "shopwithk": [product(31, "Chanel Classic Double Flap Small in Navy Caviar GHW", 480000,
                          vendor="Shop with K", tags=["chanel"])],
    "luxurywish": [product(41, "HOLD | Kelly 25 Sellier Noir Epsom GHW", 1700000,
                           vendor="Hermès")],
    "aandjluxury": [
        # vendor holds the brand; titles use "[PRE LOVED]" prefix and the
        # interleaved "Classic <size> Double Flap" word order
        product(51, "[PRE LOVED] Chanel Classic Medium Double Flap in Navy Caviar LGHW",
                520000, vendor="Chanel"),
        product(52, "[PRE LOVED] Hermes Constance 24 in Black Epsom GHW (Stamp C)",
                1100000, vendor="Hermes"),
    ],
    "missmanilaluxe": [
        # vendor = store name, tags = SKU codes => brand is read from the title
        product(61, "Chanel Double Flap Gray", 99000,
                vendor="Miss Manila Luxe", tags=["MMLD2588"]),
        product(62, "Balenciaga Classic City Purple", 49000,
                vendor="Miss Manila Luxe", tags=["MMLD2347"]),
    ],
}


def write_fixtures(catalogs):
    for k, v in catalogs.items():
        with open(os.path.join(FIXDIR, f"{k}.json"), "w") as f:
            json.dump(v, f)


def run_day(day):
    env = {**os.environ, "BAGINTEL_DB": DB, "BAGINTEL_TODAY": day}
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scraper", "scrape.py"),
                        "--from-json", FIXDIR], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def q(sql, *a):
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(sql, a).fetchall()]
    db.close()
    return rows


def main():
    if os.path.exists(DB):
        os.remove(DB)
    import copy
    cat = copy.deepcopy(BASE)

    # Day 1 — first scrape
    write_fixtures(cat)
    run_day("2026-06-01")
    assert len(q("SELECT * FROM products")) == 15   # 5+3+1+1+1+2+2 across seven stores
    r = q("SELECT * FROM products WHERE product_id=41")[0]
    assert r["status"] == "reserved", r["status"]

    # Day 2 — price drop on Kelly 28; B25 (pursemaison) flips sold-out
    cat["pursemaison"][1]["variants"][0]["price"] = "1200000"
    cat["pursemaison"][0]["variants"][0]["available"] = False
    write_fixtures(cat)
    run_day("2026-06-02")
    pc = q("SELECT * FROM price_changes")
    assert len(pc) == 1 and pc[0]["new_price"] == 1200000 and abs(pc[0]["pct"] - -7.69) < 0.1
    r = q("SELECT * FROM products WHERE product_id=1")[0]
    assert r["status"] == "sold_out" and r["sold_date"] == "2026-06-02"
    assert r["sold_price"] == 1500000 and r["days_to_sell"] == 1

    # Day 3-4 — orangebox Birkin 25 disappears (missing streak builds)
    cat["orangebox"] = [p for p in cat["orangebox"] if p["id"] != 11]
    write_fixtures(cat)
    run_day("2026-06-03")
    run_day("2026-06-04")
    r = q("SELECT * FROM products WHERE product_id=11")[0]
    assert r["status"] == "missing" and r["missing_streak"] == 2

    # Day 5 — still gone => SOLD (sold_date = first missing day)
    run_day("2026-06-05")
    r = q("SELECT * FROM products WHERE product_id=11")[0]
    assert r["status"] == "sold", r
    assert r["sold_date"] == "2026-06-03" and r["sold_price"] == 1600000
    assert r["days_to_sell"] == 2

    # Day 6 — baghub Mini Kelly disappears then re-lists with new id same title
    cat["baghub"] = [product(22, "Hermes Mini Kelly Gold Epsom GHW", 1950000,
                             vendor="The Bag Hub", tags=["Hermes"])]
    write_fixtures(cat)
    for d in ("2026-06-06", "2026-06-07", "2026-06-08"):
        run_day(d)
    old = q("SELECT * FROM products WHERE product_id=21")[0]
    new = q("SELECT * FROM products WHERE product_id=22")[0]
    assert old["status"] == "relisted", old["status"]
    assert old["sold_date"] is None
    assert new["first_seen"] == "2026-06-01", new["first_seen"]  # carried over

    # luxurywish HOLD item becomes available without HOLD => active
    cat["luxurywish"][0]["title"] = "Kelly 25 Sellier Noir Epsom GHW"
    write_fixtures(cat)
    run_day("2026-06-09")
    assert q("SELECT * FROM products WHERE product_id=41")[0]["status"] == "active"

    # ── analyze ──
    env = {**os.environ, "BAGINTEL_DB": DB, "BAGINTEL_TODAY": "2026-06-09"}
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scraper", "analyze.py")],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    with open(os.path.join(ROOT, "site", "data.json")) as f:
        data = json.load(f)

    pm = next(s for s in data["sites"] if s["key"] == "pursemaison")
    assert pm["is_mine"] and pm["inventory_count"] == 3   # 5 - sold B25 - ₱0 item
    assert pm["sold_mtd_count"] == 1 and pm["sold_mtd_value"] == 1500000
    assert pm["markdowns_mtd"] == 1 and abs(pm["avg_discount_pct"] - 7.7) < 0.1
    ob = next(s for s in data["sites"] if s["key"] == "orangebox")
    assert ob["sold_mtd_count"] == 1 and ob["sold_mtd_value"] == 1600000
    bh = next(s for s in data["sites"] if s["key"] == "baghub")
    assert bh["sold_mtd_count"] == 0      # relist must NOT count as sale
    assert bh["inventory_count"] == 1
    aj = next(s for s in data["sites"] if s["key"] == "aandjluxury")
    assert aj["inventory_count"] == 2     # 6th store ingested via vendor brand_source
    mml = next(s for s in data["sites"] if s["key"] == "missmanilaluxe")
    assert mml["inventory_count"] == 2    # 7th store; brand read from title
    mml_rows = {r["product_id"]: r for r in q("SELECT * FROM products WHERE site='missmanilaluxe'")}
    assert mml_rows[61]["brand"] == "Chanel" and mml_rows[62]["brand"] == "Balenciaga"

    heroes = {h["name"]: h for h in data["heroes"]}
    assert heroes["Birkin 25"]["per_site"]["pursemaison"]["sold_mtd"] == 1
    assert heroes["Birkin 25"]["per_site"]["orangebox"]["sold_mtd"] == 1
    assert heroes["Kelly 25"]["per_site"]["luxurywish"]["listings"], "K25 should be live"
    mk = heroes["Mini Kelly (Kelly 20)"]
    assert mk["lowest_ask"] == 1950000 and mk["live_count"] == 1
    cf = heroes["Chanel Double Flap Medium"]
    assert cf["per_site"]["pursemaison"]["listings"], "Double Flap Medium should match"
    assert heroes["Chanel Double Flap Jumbo"]["per_site"]["orangebox"]["listings"]
    # regression: "Classic DOUBLE Flap Small" must match (old 'classic\s*flap' did not)
    assert heroes["Chanel Double Flap Small"]["per_site"]["shopwithk"]["listings"], \
        "Double Flap Small should match the 'Classic Double Flap Small' title"
    # interleaved word order "Classic Medium Double Flap" (A&J Luxury) must match
    assert heroes["Chanel Double Flap Medium"]["per_site"]["aandjluxury"]["listings"], \
        "Double Flap Medium should match A&J's 'Classic Medium Double Flap' title"

    # ── benchmark tab data ──
    bmk = data["benchmark"]
    assert bmk["months"] == ["2026-06"], bmk["months"]
    assert bmk["by_site_month"]["pursemaison"]["2026-06"]["count"] == 1
    assert bmk["by_site_month"]["orangebox"]["2026-06"]["count"] == 1
    assert bmk["totals_month"]["2026-06"]["count"] == 2
    assert len(data["sold_feed"]) == 2
    assert any("Birkin 25" in x["title"] for x in data["sold_feed"])
    assert isinstance(data["insights"], list) and data["insights"]

    # ── inventory explorer / price archive ──
    with open(os.path.join(ROOT, "site", "inventory.json")) as f:
        inv = json.load(f)
    assert inv["count"] == len(q("SELECT * FROM products")), inv["count"]
    b25 = next(x for x in inv["items"]
               if x["title"].startswith("Birkin 25") and x["site"] == "pursemaison")
    assert b25["status"] in ("sold", "sold_out") and b25["history"], b25  # sold item keeps history
    live = next(x for x in inv["items"] if x["site"] == "missmanilaluxe")
    assert live["history"], live  # live item has a price archive too

    # ── AI agent: no key => safe placeholder, exit 0 (never breaks the pipeline) ──
    env_nokey = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scraper", "ai_brief.py")],
                       env=env_nokey, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    with open(os.path.join(ROOT, "site", "briefing.json")) as f:
        brief = json.load(f)
    assert brief["configured"] is False and "briefing" in brief

    # attribute extraction spot checks
    rows = {r["product_id"]: r for r in q("SELECT * FROM products WHERE site='pursemaison'")}
    assert rows[1]["hardware"] == "Gold" and rows[1]["leather"] == "Togo"
    assert rows[2]["color"] == "Etoupe" and rows[2]["leather"] == "Epsom"
    assert rows[3]["brand"] == "Chanel"
    ob_rows = {r["product_id"]: r for r in q("SELECT * FROM products WHERE site='orangebox'")}
    assert ob_rows[13]["brand"] == "Louis Vuitton"

    # ── excel ──
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scraper", "export_excel.py")],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    import glob
    xs = glob.glob(os.path.join(ROOT, "exports", "*.xlsx"))
    assert xs, "no excel produced"

    print("ALL TESTS PASSED ✔")
    shutil.rmtree(FIXDIR, ignore_errors=True)


if __name__ == "__main__":
    main()

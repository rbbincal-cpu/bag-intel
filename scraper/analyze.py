"""Compute all dashboard metrics from SQLite -> site/data.json"""
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, get_db, load_config, manila_today

LIVE = ("active", "reserved", "sold_out")   # still listed on site
UNSOLD = ("active", "reserved")             # countable inventory


def hero_matchers(cfg):
    out = []
    for hm in cfg["hero_models"]:
        pats = [re.compile(k, re.I) for k in hm["keywords"]]
        out.append((hm["name"], hm["brand"], pats))
    return out


def match_hero(matchers, brand, nt):
    for name, hbrand, pats in matchers:
        if hbrand.lower() not in (brand or "").lower().replace("è", "e"):
            # allow Hermès/Hermes
            if not (hbrand == "Hermes" and "herm" in (brand or "").lower()):
                continue
        for p in pats:
            if p.search(nt or ""):
                return name
    return None


def month_start(d):
    return d[:8] + "01"


def main():
    cfg = load_config()
    db = get_db()
    today = manila_today(cfg)
    mtd_start = month_start(today)
    t = date.fromisoformat(today)
    d60 = (t - timedelta(days=60)).isoformat()
    d90 = (t - timedelta(days=90)).isoformat()
    matchers = hero_matchers(cfg)
    maxp = cfg.get("max_valid_price", 50000000)
    valid = lambda x: x is not None and 0 < x < maxp
    vp = lambda x: x if valid(x) else 0

    # wall-clock Manila timestamp of THIS run (date + time), for the dashboard header
    generated_at = datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Manila"))).strftime("%Y-%m-%d %H:%M")

    sites = {s["key"]: s for s in cfg["sites"]}
    data = {"generated": today, "generated_at": generated_at,
            "currency": cfg.get("currency", "PHP"),
            "sites": [], "heroes": [], "series": {}, "competitor_detail": {}}

    prods = [dict(r) for r in db.execute("SELECT * FROM products").fetchall()]
    by_site = defaultdict(list)
    for p in prods:
        p["hero"] = match_hero(matchers, p["brand"], p["norm_title"]) \
            if p["category"] == "Bags" else None
        by_site[p["site"]].append(p)

    # first run date per site (items first_seen that day predate tracking)
    first_run = {k: min((p["first_seen"] for p in v), default=today)
                 for k, v in by_site.items()}

    for key, site in sites.items():
        ps = by_site.get(key, [])
        inv = [p for p in ps if p["status"] in UNSOLD and valid(p["current_price"])]
        sold_mtd = [p for p in ps if p["status"] in ("sold", "sold_out")
                    and p["sold_date"] and p["sold_date"] >= mtd_start]
        new_mtd = [p for p in ps if p["first_seen"] and p["first_seen"] >= mtd_start
                   and p["first_seen"] > first_run[key]]
        # brand mix of live inventory
        mix = defaultdict(float)
        for p in inv:
            b = p["brand"] or "Other"
            grp = b if b in ("Hermès", "Chanel", "Louis Vuitton") else "Other"
            mix[grp] += p["current_price"]
        tot_val = sum(p["current_price"] for p in inv)

        # upload cadence by weekday (all history)
        cadence = [0] * 7
        for p in ps:
            if p["first_seen"] and p["first_seen"] > first_run[key]:
                cadence[date.fromisoformat(p["first_seen"]).weekday()] += 1

        # markdowns MTD
        marks = db.execute("""SELECT * FROM price_changes WHERE site=? AND date>=?
                              AND pct<0""", (key, mtd_start)).fetchall()
        ups = db.execute("""SELECT COUNT(*) c FROM price_changes WHERE site=? AND
                            date>=? AND pct>0""", (key, mtd_start)).fetchone()["c"]

        aging60 = [p for p in inv if p["first_seen"] and p["first_seen"] <= d60]
        aging90 = [p for p in inv if p["first_seen"] and p["first_seen"] <= d90]

        dts = [p["days_to_sell"] for p in ps
               if p["days_to_sell"] is not None and p["status"] in ("sold", "sold_out")]

        sold_brands = defaultdict(lambda: [0, 0.0])
        sold_models = defaultdict(lambda: [0, 0.0])
        for p in sold_mtd:
            sp = vp(p["sold_price"])
            sold_brands[p["brand"] or "Other"][0] += 1
            sold_brands[p["brand"] or "Other"][1] += sp
            m = f'{p["brand"] or ""} {p["model"] or "Other"}'.strip()
            sold_models[m][0] += 1
            sold_models[m][1] += sp

        data["sites"].append({
            "key": key, "name": site["name"], "is_mine": site.get("is_mine", False),
            "url": site["base_url"],
            "inventory_count": len(inv),
            "inventory_value": round(tot_val),
            "avg_ticket": round(tot_val / len(inv)) if inv else 0,
            "brand_mix": {k: round(v / tot_val * 100, 1) if tot_val else 0
                          for k, v in sorted(mix.items())},
            "sold_mtd_count": len(sold_mtd),
            "sold_mtd_value": round(sum(vp(p["sold_price"]) for p in sold_mtd)),
            "sold_mtd_brands": {k: {"count": v[0], "value": round(v[1])}
                                for k, v in sorted(sold_brands.items(),
                                                   key=lambda x: -x[1][1])},
            "sold_mtd_models": {k: {"count": v[0], "value": round(v[1])}
                                for k, v in sorted(sold_models.items(),
                                                   key=lambda x: -x[1][1])[:15]},
            "new_mtd_count": len(new_mtd),
            "new_mtd_value": round(sum(vp(p["current_price"]) for p in new_mtd)),
            "cadence": cadence,
            "aging60_count": len(aging60), "aging60_value": round(sum(p["current_price"] for p in aging60)),
            "aging90_count": len(aging90), "aging90_value": round(sum(p["current_price"] for p in aging90)),
            "markdowns_mtd": len(marks),
            "markups_mtd": ups,
            "avg_discount_pct": round(sum(-m["pct"] for m in marks) / len(marks), 1) if marks else 0,
            "avg_days_to_sell": round(sum(dts) / len(dts), 1) if dts else None,
            "tracking_since": first_run[key],
        })

        # per-competitor detail: live listings + recent solds + price drops
        detail_inv = sorted(inv, key=lambda p: -(p["current_price"] or 0))
        data["competitor_detail"][key] = {
            "listings": [{
                "title": p["title"], "url": p["url"], "brand": p["brand"],
                "model": p["model"], "price": p["current_price"],
                "color": p["color"], "hardware": p["hardware"],
                "leather": p["leather"], "size": p["size"],
                "category": p["category"], "status": p["status"],
                "first_seen": p["first_seen"], "hero": p["hero"],
                "age_days": (t - date.fromisoformat(p["first_seen"])).days
                            if p["first_seen"] else None,
            } for p in detail_inv],
            "recent_sold": [{
                "title": p["title"], "brand": p["brand"], "model": p["model"],
                "sold_price": p["sold_price"], "sold_date": p["sold_date"],
                "days_to_sell": p["days_to_sell"], "url": p["url"],
            } for p in sorted(
                [p for p in ps if p["status"] in ("sold", "sold_out") and p["sold_date"]],
                key=lambda x: x["sold_date"], reverse=True)[:60]],
            "price_drops_mtd": [{
                "date": m["date"], "old": m["old_price"], "new": m["new_price"],
                "pct": m["pct"],
                "title": next((q["title"] for q in ps if q["product_id"] == m["product_id"]), ""),
            } for m in marks][:60],
        }

    # ── hero models ─────────────────────────────────────────────────
    for hm in cfg["hero_models"]:
        name = hm["name"]
        entry = {"name": name, "brand": hm["brand"], "per_site": {}, "all_prices": []}
        for key, site in sites.items():
            ps = [p for p in by_site.get(key, []) if p["hero"] == name]
            live = [p for p in ps if p["status"] in UNSOLD and valid(p["current_price"])]
            sold_mtd = [p for p in ps if p["status"] in ("sold", "sold_out")
                        and p["sold_date"] and p["sold_date"] >= mtd_start]
            sold_all = [p for p in ps if p["status"] in ("sold", "sold_out")
                        and valid(p["sold_price"])]
            dts = [p["days_to_sell"] for p in ps if p["days_to_sell"] is not None]
            entry["per_site"][key] = {
                "site_name": site["name"], "is_mine": site.get("is_mine", False),
                "listings": [{
                    "title": p["title"], "url": p["url"], "price": p["current_price"],
                    "color": p["color"], "hardware": p["hardware"],
                    "leather": p["leather"], "status": p["status"],
                    "age_days": (t - date.fromisoformat(p["first_seen"])).days
                                if p["first_seen"] else None,
                } for p in sorted(live, key=lambda x: x["current_price"] or 0)],
                "sold_mtd": len(sold_mtd),
                "avg_sold_price": round(sum(p["sold_price"] or 0 for p in sold_all)
                                        / len(sold_all)) if sold_all else None,
                "avg_days_to_sell": round(sum(dts) / len(dts), 1) if dts else None,
            }
            entry["all_prices"] += [p["current_price"] for p in live]
        entry["lowest_ask"] = min(entry["all_prices"]) if entry["all_prices"] else None
        entry["highest_ask"] = max(entry["all_prices"]) if entry["all_prices"] else None
        entry["live_count"] = len(entry["all_prices"])
        del entry["all_prices"]
        data["heroes"].append(entry)

    # ── time series ─────────────────────────────────────────────────
    # inventory value & count per site per day (from snapshots, available only)
    rows = db.execute("""SELECT date, site, COUNT(*) c, SUM(price) v FROM snapshots
                         WHERE available=1 AND price>0 AND price<? GROUP BY date, site
                         ORDER BY date""", (maxp,)).fetchall()
    series = defaultdict(lambda: {"dates": [], "value": [], "count": []})
    for r in rows:
        s = series[r["site"]]
        s["dates"].append(r["date"])
        s["value"].append(round(r["v"] or 0))
        s["count"].append(r["c"])
    data["series"]["inventory"] = series

    # sold per day per site (for MTD trend), new uploads per day
    sold_rows = db.execute("""SELECT sold_date d, site, COUNT(*) c,
                              SUM(CASE WHEN COALESCE(sold_price,0)<? THEN
                                  COALESCE(sold_price,0) ELSE 0 END) v FROM products
                              WHERE status IN ('sold','sold_out') AND sold_date
                              IS NOT NULL GROUP BY sold_date, site""", (maxp,)).fetchall()
    data["series"]["sold"] = [dict(r) for r in sold_rows]
    new_rows = db.execute("""SELECT first_seen d, site, COUNT(*) c,
                             SUM(CASE WHEN COALESCE(current_price,0)<? THEN
                                 COALESCE(current_price,0) ELSE 0 END) v FROM products
                             GROUP BY first_seen, site""", (maxp,)).fetchall()
    # exclude each site's first-run bulk import from "new uploads"
    data["series"]["new"] = [dict(r) for r in new_rows
                             if r["d"] and r["d"] > first_run.get(r["site"], "")]

    # ── benchmark: monthly sold-per-store, sold-product feed, insights ──────
    pesoF = lambda n: "₱{:,}".format(int(round(n)))
    cur = today[:7]
    all_sold = [p for p in prods if p["status"] in ("sold", "sold_out") and p["sold_date"]]

    by_site_month = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    months_set = set()
    for p in all_sold:
        m = p["sold_date"][:7]
        months_set.add(m)
        cell = by_site_month[p["site"]][m]
        cell[0] += 1
        cell[1] += vp(p["sold_price"])
    months = sorted(months_set)
    totals_month = {m: {"count": 0, "value": 0.0} for m in months}
    bsm_json = {}
    for skey, mm in by_site_month.items():
        bsm_json[skey] = {}
        for m, (c, v) in mm.items():
            bsm_json[skey][m] = {"count": c, "value": round(v)}
            totals_month[m]["count"] += c
            totals_month[m]["value"] += v
    for m in totals_month:
        totals_month[m]["value"] = round(totals_month[m]["value"])
    data["benchmark"] = {"months": months, "by_site_month": bsm_json,
                         "totals_month": totals_month}

    data["sold_feed"] = [{
        "site": p["site"], "site_name": sites[p["site"]]["name"],
        "is_mine": sites[p["site"]].get("is_mine", False),
        "title": p["title"], "brand": p["brand"], "model": p["model"],
        "sold_price": p["sold_price"] if valid(p["sold_price"]) else None,
        "sold_date": p["sold_date"], "days_to_sell": p["days_to_sell"],
        "url": p["url"],
    } for p in sorted(all_sold, key=lambda x: x["sold_date"], reverse=True)[:400]]

    # data-derived business insights for the current month
    insights = []
    cur_sold = [p for p in all_sold if p["sold_date"][:7] == cur]
    pm_key = next((k for k, s in sites.items() if s.get("is_mine")), None)
    pm_name = sites[pm_key]["name"] if pm_key else "your store"

    site_cnt = Counter(p["site"] for p in cur_sold)
    if site_cnt:
        ranked = site_cnt.most_common()
        lk, ln = ranked[0]
        txt = f"{sites[lk]['name']} leads this month with {ln} sold."
        if pm_key:
            pm_n = site_cnt.get(pm_key, 0)
            pm_rank = next((i + 1 for i, (k, _) in enumerate(ranked) if k == pm_key), None)
            txt += (f" {pm_name}: {pm_n} sold (#{pm_rank} of {len(sites)})." if pm_n
                    else f" {pm_name} has no recorded sales yet this month.")
        insights.append({"cat": "Sell-through", "text": txt})
        site_dts = defaultdict(list)
        for p in cur_sold:
            if p["days_to_sell"] is not None:
                site_dts[p["site"]].append(p["days_to_sell"])
        avg_dts = {k: sum(v) / len(v) for k, v in site_dts.items() if v}
        if avg_dts:
            fk = min(avg_dts, key=avg_dts.get)
            insights.append({"cat": "Sell-through",
                "text": f"Fastest turnaround: {sites[fk]['name']} at {avg_dts[fk]:.1f} days avg to sell."})

    bm_cnt = Counter(f"{p['brand'] or ''} {p['model'] or ''}".strip() or "Other" for p in cur_sold)
    if bm_cnt:
        top = ", ".join(f"{lbl} ({c})" for lbl, c in bm_cnt.most_common(3))
        insights.append({"cat": "Top sellers", "text": f"Hottest models this month: {top}."})

    for h in data["heroes"]:
        ps = h["per_site"].get(pm_key, {}) if pm_key else {}
        yours = [l["price"] for l in ps.get("listings", []) if l.get("price")]
        other = [l["price"] for k, v in h["per_site"].items() if k != pm_key
                 for l in v.get("listings", []) if l.get("price")]
        if yours and other:
            ymin, omin = min(yours), min(other)
            if omin:
                pct = (ymin - omin) / omin * 100
                pos = "above" if pct >= 0 else "below"
                insights.append({"cat": "Pricing",
                    "text": f"{h['name']}: your ask {pesoF(ymin)} is {abs(pct):.0f}% {pos} the market low {pesoF(omin)}."})

    if pm_key:
        pm_inv = [p for p in by_site.get(pm_key, [])
                  if p["status"] in UNSOLD and valid(p["current_price"])]
        pm_aging = [p for p in pm_inv if p["first_seen"] and p["first_seen"] <= d90]
        if pm_aging:
            val = sum(p["current_price"] for p in pm_aging)
            insights.append({"cat": "Inventory",
                "text": f"{len(pm_aging)} of your listings have sat 90+ days (~{pesoF(val)} tied up). Consider markdowns or promotion."})
        mkt_bm = Counter((p["brand"], p["model"]) for p in cur_sold if p["model"])
        pm_live_bm = {(p["brand"], p["model"]) for p in pm_inv}
        restock = [bm for bm, c in mkt_bm.most_common() if c >= 2 and bm not in pm_live_bm]
        if restock:
            names = ", ".join(f"{b or ''} {m or ''}".strip() for b, m in restock[:3])
            insights.append({"cat": "Inventory",
                "text": f"Selling across the market but missing from your shelves: {names}. Restock candidates."})

    if not insights:
        insights.append({"cat": "Heads-up",
            "text": "Not enough sales history yet for insights — these populate as data accumulates over the coming weeks."})
    data["insights"] = insights

    out = os.path.join(ROOT, "site", "data.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {out} ({os.path.getsize(out)//1024} KB)")

    # ── inventory explorer + price archive (separate file; the dashboard stays light) ──
    # Compact price timeline per product, straight from the daily snapshots: keep
    # only the points where the price changed. Sold/delisted items are retained
    # forever, so this is a permanent price archive for pricing/consignment calls.
    hist = defaultdict(list)  # (site, product_id) -> [[date, price], ...] change-points
    for r in db.execute("""SELECT site, product_id, date, price FROM snapshots
                           WHERE price > 0 AND price < ? ORDER BY site, product_id, date""",
                        (maxp,)):
        pts = hist[(r["site"], r["product_id"])]
        if not pts or pts[-1][1] != r["price"]:
            pts.append([r["date"], r["price"]])

    items = []
    for p in prods:
        pts = list(hist.get((p["site"], p["product_id"]), []))
        if p["sold_date"] and valid(p["sold_price"]):
            if not pts or pts[-1][0] != p["sold_date"]:
                pts.append([p["sold_date"], round(p["sold_price"])])
        sold = p["status"] in ("sold", "sold_out") and valid(p["sold_price"])
        last_price = p["sold_price"] if sold else p["current_price"]
        end_date = p["sold_date"] or today
        age = ((date.fromisoformat(end_date) - date.fromisoformat(p["first_seen"])).days
               if p["first_seen"] else None)
        items.append({
            "site": p["site"], "store": sites[p["site"]]["name"],
            "is_mine": sites[p["site"]].get("is_mine", False),
            "title": p["title"], "url": p["url"],
            "brand": p["brand"], "model": p["model"], "hero": p["hero"],
            "color": p["color"], "hardware": p["hardware"], "leather": p["leather"],
            "size": p["size"], "category": p["category"], "status": p["status"],
            "price": round(last_price) if valid(last_price) else None,
            "first_seen": p["first_seen"], "sold_date": p["sold_date"],
            "days_to_sell": p["days_to_sell"], "age_days": age,
            "history": [[d, round(pr)] for d, pr in pts],
        })
    inv = {"generated_at": generated_at, "currency": cfg.get("currency", "PHP"),
           "count": len(items), "items": items}
    invout = os.path.join(ROOT, "site", "inventory.json")
    with open(invout, "w", encoding="utf-8") as f:
        json.dump(inv, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {invout} ({os.path.getsize(invout)//1024} KB, {len(items)} items)")

    db.close()


if __name__ == "__main__":
    main()

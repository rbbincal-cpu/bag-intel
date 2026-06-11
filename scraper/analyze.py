"""Compute all dashboard metrics from SQLite -> site/data.json"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, timedelta

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

    sites = {s["key"]: s for s in cfg["sites"]}
    data = {"generated": today, "currency": cfg.get("currency", "PHP"),
            "sites": [], "heroes": [], "series": {}, "competitor_detail": {}}

    prods = [dict(r) for r in db.execute("SELECT * FROM products").fetchall()]
    by_site = defaultdict(list)
    for p in prods:
        p["hero"] = match_hero(matchers, p["brand"], p["norm_title"])
        by_site[p["site"]].append(p)

    # first run date per site (items first_seen that day predate tracking)
    first_run = {k: min((p["first_seen"] for p in v), default=today)
                 for k, v in by_site.items()}

    for key, site in sites.items():
        ps = by_site.get(key, [])
        inv = [p for p in ps if p["status"] in UNSOLD and (p["current_price"] or 0) > 0]
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
            sp = p["sold_price"] or 0
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
            "sold_mtd_value": round(sum(p["sold_price"] or 0 for p in sold_mtd)),
            "sold_mtd_brands": {k: {"count": v[0], "value": round(v[1])}
                                for k, v in sorted(sold_brands.items(),
                                                   key=lambda x: -x[1][1])},
            "sold_mtd_models": {k: {"count": v[0], "value": round(v[1])}
                                for k, v in sorted(sold_models.items(),
                                                   key=lambda x: -x[1][1])[:15]},
            "new_mtd_count": len(new_mtd),
            "new_mtd_value": round(sum(p["current_price"] or 0 for p in new_mtd)),
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
            live = [p for p in ps if p["status"] in UNSOLD and (p["current_price"] or 0) > 0]
            sold_mtd = [p for p in ps if p["status"] in ("sold", "sold_out")
                        and p["sold_date"] and p["sold_date"] >= mtd_start]
            sold_all = [p for p in ps if p["status"] in ("sold", "sold_out") and p["sold_price"]]
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
                         WHERE available=1 AND price>0 GROUP BY date, site
                         ORDER BY date""").fetchall()
    series = defaultdict(lambda: {"dates": [], "value": [], "count": []})
    for r in rows:
        s = series[r["site"]]
        s["dates"].append(r["date"])
        s["value"].append(round(r["v"] or 0))
        s["count"].append(r["c"])
    data["series"]["inventory"] = series

    # sold per day per site (for MTD trend), new uploads per day
    sold_rows = db.execute("""SELECT sold_date d, site, COUNT(*) c,
                              SUM(COALESCE(sold_price,0)) v FROM products
                              WHERE status IN ('sold','sold_out') AND sold_date
                              IS NOT NULL GROUP BY sold_date, site""").fetchall()
    data["series"]["sold"] = [dict(r) for r in sold_rows]
    new_rows = db.execute("""SELECT first_seen d, site, COUNT(*) c,
                             SUM(COALESCE(current_price,0)) v FROM products
                             GROUP BY first_seen, site""").fetchall()
    # exclude each site's first-run bulk import from "new uploads"
    data["series"]["new"] = [dict(r) for r in new_rows
                             if r["d"] and r["d"] > first_run.get(r["site"], "")]

    out = os.path.join(ROOT, "site", "data.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {out} ({os.path.getsize(out)//1024} KB)")
    db.close()


if __name__ == "__main__":
    main()

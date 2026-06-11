"""Daily scrape: fetch all Shopify catalogs, snapshot into SQLite, run
sold/price-change/relist detection.

Usage:
  python scraper/scrape.py                 # live scrape
  python scraper/scrape.py --from-json DIR # ingest pre-fetched {sitekey}.json dumps
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.robotparser

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (get_db, load_config, manila_today, detect_brand, detect_model,
                    detect_category, extract_attrs, norm_title, is_reserved,
                    strip_html, fingerprint)

try:
    from PIL import Image
    import imagehash
    HAS_IMAGEHASH = True
except ImportError:
    HAS_IMAGEHASH = False


def session_for(cfg):
    s = requests.Session()
    s.headers["User-Agent"] = cfg["scrape"]["user_agent"]
    return s


def robots_allows(sess, base_url, path="/products.json"):
    try:
        rp = urllib.robotparser.RobotFileParser()
        r = sess.get(base_url.rstrip("/") + "/robots.txt", timeout=15)
        rp.parse(r.text.splitlines())
        return rp.can_fetch(sess.headers["User-Agent"], base_url.rstrip("/") + path)
    except Exception:
        return True  # fail open, endpoint is a public API


def fetch_catalog(sess, site, cfg):
    sc = cfg["scrape"]
    products = []
    for page in range(1, sc["max_pages"] + 1):
        url = f"{site['base_url'].rstrip('/')}/products.json"
        for attempt in range(sc["retries"]):
            try:
                r = sess.get(url, params={"limit": sc["page_limit"], "page": page},
                             timeout=sc["timeout_seconds"])
                r.raise_for_status()
                batch = r.json().get("products", [])
                break
            except Exception:
                if attempt == sc["retries"] - 1:
                    raise
                time.sleep(5 * (attempt + 1))
        if not batch:
            break
        products.extend(batch)
        time.sleep(sc["delay_seconds"])
    return products


def image_hash_for(sess, src, width):
    if not (HAS_IMAGEHASH and src):
        return None
    try:
        sep = "&" if "?" in src else "?"
        r = sess.get(f"{src}{sep}width={width}", timeout=20)
        r.raise_for_status()
        return str(imagehash.dhash(Image.open(io.BytesIO(r.content)).convert("RGB")))
    except Exception:
        return None


def product_price(p):
    """Max variant price (single-variant stores anyway); 0 => placeholder."""
    prices = [float(v.get("price") or 0) for v in p.get("variants", [])]
    return max(prices) if prices else 0.0


def product_available(p):
    return any(v.get("available") for v in p.get("variants", []))


def ingest_site(db, site, products, today, cfg, sess=None):
    sc = cfg["scrape"]
    key = site["key"]
    seen_ids = set()
    new_rows = []

    for p in products:
        pid = p["id"]
        seen_ids.add(pid)
        price = product_price(p)
        avail = product_available(p)
        title = p.get("title") or ""
        nt = norm_title(title)

        db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)",
                   (today, key, pid, price, int(avail), title))

        row = db.execute("SELECT * FROM products WHERE site=? AND product_id=?",
                         (key, pid)).fetchone()
        if row is None:
            body = strip_html(p.get("body_html") or "")[:800]
            attrs = extract_attrs(title, body)
            brand = detect_brand(p, site.get("brand_source", "title"))
            model = detect_model(title)
            cat = detect_category(title, p.get("product_type"))
            img = (p.get("images") or [{}])[0].get("src")
            ih = image_hash_for(sess, img, sc.get("image_hash_width", 128)) \
                if (sess and sc.get("image_hash")) else None
            status = "reserved" if is_reserved(title) else \
                     ("sold_out" if not avail else "active")
            db.execute("""INSERT INTO products
                (site, product_id, handle, url, title, norm_title, brand, model,
                 color, hardware, leather, size, category,
                 first_seen, last_seen, published_at, created_at,
                 current_price, status, initial_sold_out, image_src, image_hash, fingerprint)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, pid, p.get("handle"),
                 f"{site['base_url'].rstrip('/')}/products/{p.get('handle')}",
                 title, nt, brand, model,
                 attrs["color"], attrs["hardware"], attrs["leather"], attrs["size"], cat,
                 today, today, p.get("published_at"), p.get("created_at"),
                 price, status, int(not avail), img, ih, fingerprint(nt, ih, p.get("handle"))))
            new_rows.append((pid, nt, ih))
            continue

        # existing product ───────────────────────────────────────────
        updates = {"last_seen": today, "missing_streak": 0, "first_missing_date": None}
        old_price = row["current_price"] or 0

        if price > 0 and old_price > 0 and abs(price - old_price) >= 0.01:
            db.execute("INSERT INTO price_changes VALUES (?,?,?,?,?,?)",
                       (key, pid, today, old_price, price,
                        round((price - old_price) / old_price * 100, 2)))
        if price > 0:
            updates["current_price"] = price

        if row["status"] in ("active", "reserved", "missing"):
            if not avail:
                # transitioned to sold-out => SOLD
                updates.update(status="sold_out", sold_date=today,
                               sold_price=old_price or price)
                if row["first_seen"]:
                    from datetime import date
                    d0 = date.fromisoformat(row["first_seen"])
                    updates["days_to_sell"] = (date.fromisoformat(today) - d0).days
            elif is_reserved(title) and row["status"] != "reserved":
                updates["status"] = "reserved"
            elif not is_reserved(title) and row["status"] in ("reserved", "missing"):
                updates["status"] = "active"
        elif row["status"] == "sold_out" and avail:
            updates["status"] = "active"  # restocked / un-sold
            updates.update(sold_date=None, sold_price=None, days_to_sell=None)

        if title != row["title"]:
            updates["title"] = title
            updates["norm_title"] = nt

        sets = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE products SET {sets} WHERE pk=?",
                   (*updates.values(), row["pk"]))

    # ── disappearance handling ──────────────────────────────────────
    gone = db.execute("""SELECT * FROM products WHERE site=? AND status IN
        ('active','reserved','missing') AND product_id NOT IN
        (SELECT product_id FROM snapshots WHERE site=? AND date=?)""",
        (key, key, today)).fetchall()

    threshold = cfg["sold_detection"]["missing_days_threshold"]
    for row in gone:
        streak = (row["missing_streak"] or 0) + 1
        first_missing = row["first_missing_date"] or today
        if streak >= threshold:
            # check re-list: a later-arriving listing with same title/image,
            # whose first snapshot falls on/after the day this one vanished
            np = db.execute("""SELECT p.pk, p.product_id FROM products p
                WHERE p.site=? AND p.pk>? AND p.status NOT IN ('relisted')
                  AND (p.norm_title=? OR (p.image_hash IS NOT NULL AND p.image_hash=?))
                  AND (SELECT MIN(s.date) FROM snapshots s
                       WHERE s.site=p.site AND s.product_id=p.product_id)
                      >= date(?, '-1 day')
                LIMIT 1""",
                (key, row["pk"], row["norm_title"], row["image_hash"] or "?none?",
                 first_missing)).fetchone()
            if np:
                # carry original first_seen to the new listing; don't count as sold
                db.execute("UPDATE products SET first_seen=? WHERE pk=?",
                           (row["first_seen"], np["pk"]))
                db.execute("""UPDATE products SET status='relisted', relisted_to=?,
                              missing_streak=? WHERE pk=?""",
                           (np["pk"], streak, row["pk"]))
            else:
                from datetime import date
                dts = None
                if row["first_seen"]:
                    dts = (date.fromisoformat(first_missing)
                           - date.fromisoformat(row["first_seen"])).days
                db.execute("""UPDATE products SET status='sold', sold_date=?,
                              sold_price=?, days_to_sell=?, missing_streak=?,
                              first_missing_date=? WHERE pk=?""",
                           (first_missing, row["current_price"], dts, streak,
                            first_missing, row["pk"]))
        else:
            db.execute("""UPDATE products SET status='missing', missing_streak=?,
                          first_missing_date=? WHERE pk=?""",
                       (streak, first_missing, row["pk"]))

    # also: new product matching a recently-sold item within threshold window =>
    # un-sell the old one (it was a relist, not a sale)
    for npid, nnt, nih in new_rows:
        if not nnt:
            continue
        prior = db.execute("""SELECT pk, first_seen FROM products WHERE site=? AND
            status='sold' AND norm_title=? AND product_id<>? AND
            julianday(?)-julianday(sold_date) <= 14""",
            (key, nnt, npid, today)).fetchone()
        if prior:
            np = db.execute("SELECT pk FROM products WHERE site=? AND product_id=?",
                            (key, npid)).fetchone()
            db.execute("UPDATE products SET first_seen=? WHERE pk=?",
                       (prior["first_seen"], np["pk"]))
            db.execute("""UPDATE products SET status='relisted', relisted_to=?,
                          sold_date=NULL, sold_price=NULL, days_to_sell=NULL
                          WHERE pk=?""", (np["pk"], prior["pk"]))

    return len(seen_ids), len(new_rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", help="dir with {sitekey}.json raw product dumps")
    ap.add_argument("--site", help="only this site key")
    args = ap.parse_args()

    cfg = load_config()
    today = manila_today(cfg)
    db = get_db()
    failures = []

    for site in cfg["sites"]:
        if args.site and site["key"] != args.site:
            continue
        key = site["key"]
        try:
            if args.from_json:
                with open(os.path.join(args.from_json, f"{key}.json"), encoding="utf-8") as f:
                    products = json.load(f)
                sess = None
            else:
                sess = session_for(cfg)
                if cfg["scrape"].get("respect_robots") and not robots_allows(sess, site["base_url"]):
                    raise RuntimeError("robots.txt disallows /products.json")
                products = fetch_catalog(sess, site, cfg)
            n, new = ingest_site(db, site, products, today, cfg, sess)
            db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,1,NULL)", (today, key, n))
            print(f"[{key}] OK — {n} products ({new} new)")
        except Exception as e:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,0,0,?)", (today, key, str(e)))
            print(f"[{key}] FAILED — {e}", file=sys.stderr)
            failures.append(key)
        db.commit()

    db.commit()
    db.close()
    if failures:
        sys.exit(f"Scrape failures: {', '.join(failures)}")


if __name__ == "__main__":
    main()

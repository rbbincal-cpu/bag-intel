"""AI analyst agent: turns site/data.json into a business briefing via the
Anthropic API and writes site/briefing.json (briefing / alerts / pricing).

Safe by design:
  * No ANTHROPIC_API_KEY  -> writes a "not configured" placeholder, exits 0.
  * Any error            -> writes a fallback note, exits 0.
It NEVER fails the pipeline. Run after analyze.py.

Run:  python scraper/ai_brief.py
"""
import json
import os
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, load_config

API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM_PROMPT = (
    "You are a sharp luxury-resale market analyst for Purse Maison, a pre-owned "
    "designer handbag store in Manila. You are given a daily competitive snapshot — "
    "your store (is_mine=true) plus competitors. Competitor 'sold' figures are INFERRED "
    "from de-listings (a directional signal, not audited revenue); your own numbers are "
    "exact. Give concise, decision-ready guidance with real numbers and store names. "
    "Currency is Philippine peso. Return ONLY a JSON object (no prose, no markdown) with "
    "exactly these keys:\n"
    '  "briefing": array of up to 6 objects, each {"title": short, "detail": one sentence '
    'with concrete numbers, "action": one specific next step}\n'
    '  "alerts": array of up to 5 short strings — unusual moves, opportunities, or threats\n'
    '  "pricing": array of up to 6 objects, each {"model": name, "suggestion": one sentence '
    "price guidance vs the market}\n"
    "Prioritise pricing, sell-through, restock and markdown decisions. If the data is thin "
    "(early in tracking), say so briefly rather than inventing trends."
)


def manila_now(cfg):
    return datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Manila"))).strftime("%Y-%m-%d %H:%M")


def write(obj):
    out = os.path.join(ROOT, "site", "briefing.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {out}")


def summarize(data):
    """Compact, token-light snapshot for the model."""
    mine_key = next((s["key"] for s in data["sites"] if s.get("is_mine")), None)
    stores = [{
        "store": s["name"], "is_mine": s.get("is_mine", False),
        "inventory": s.get("inventory_count"), "inv_value": s.get("inventory_value"),
        "avg_ticket": s.get("avg_ticket"), "sold_mtd": s.get("sold_mtd_count"),
        "sold_value_mtd": s.get("sold_mtd_value"), "avg_days_to_sell": s.get("avg_days_to_sell"),
        "aging90": s.get("aging90_count", 0), "brand_mix_pct": s.get("brand_mix", {}),
        "top_sold_models_mtd": list((s.get("sold_mtd_models") or {}).keys())[:5],
    } for s in data.get("sites", [])]
    heroes = []
    for h in data.get("heroes", []):
        yours = [l["price"] for l in h["per_site"].get(mine_key, {}).get("listings", [])
                 if l.get("price")] if mine_key else []
        heroes.append({
            "model": h.get("name"), "your_live_count": len(yours),
            "your_lowest_ask": min(yours) if yours else None,
            "market_lowest_ask": h.get("lowest_ask"), "market_highest_ask": h.get("highest_ask"),
            "market_live_count": h.get("live_count"),
        })
    return {"month": (data.get("generated") or "")[:7], "stores": stores, "hero_models": heroes}


def call_claude(api_key, model, snapshot):
    body = {
        "model": model, "max_tokens": 4096, "system": SYSTEM_PROMPT,   # 1600 truncated the JSON mid-string
        "messages": [{"role": "user", "content": "Daily snapshot JSON:\n" + json.dumps(snapshot)}],
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.load(r)
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
    if text.startswith("```"):                      # strip ```json ... ``` fences if present
        text = text.strip("`")
        text = text[4:].strip() if text.lower().startswith("json") else text
    return json.loads(text)


def main():
    cfg = load_config()
    now = manila_now(cfg)
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()   # tolerate stray whitespace/newline
    model = (os.environ.get("AI_MODEL") or "claude-sonnet-4-6").strip()

    if not key:
        write({"generated_at": now, "configured": False,
               "note": "AI briefing not configured. Add the ANTHROPIC_API_KEY repository "
                       "secret to switch on the analyst agent.",
               "briefing": [], "alerts": [], "pricing": []})
        return

    try:
        with open(os.path.join(ROOT, "site", "data.json"), encoding="utf-8") as f:
            data = json.load(f)
        result = call_claude(key, model, summarize(data))
        write({"generated_at": now, "configured": True, "model": model,
               "briefing": result.get("briefing", []) or [],
               "alerts": result.get("alerts", []) or [],
               "pricing": result.get("pricing", []) or []})
        print("AI briefing generated.")
    except Exception as e:                            # never break the pipeline
        print(f"AI briefing failed: {e}", file=sys.stderr)
        write({"generated_at": now, "configured": True, "error": True,
               "note": "The AI briefing could not be generated this run; it will retry next run.",
               "briefing": [], "alerts": [], "pricing": []})


if __name__ == "__main__":
    main()

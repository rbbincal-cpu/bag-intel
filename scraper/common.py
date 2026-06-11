"""Shared helpers: config, DB, normalization, attribute extraction."""
import html
import os
import re
import sqlite3
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("BAGINTEL_DB") or os.path.join(ROOT, "data", "market.db")
CONFIG_PATH = os.path.join(ROOT, "config.yaml")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def manila_today(cfg=None):
    if os.environ.get("BAGINTEL_TODAY"):       # test override
        return os.environ["BAGINTEL_TODAY"]
    tz = ZoneInfo((cfg or load_config()).get("timezone", "Asia/Manila"))
    return datetime.now(tz).date().isoformat()


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  pk INTEGER PRIMARY KEY AUTOINCREMENT,
  site TEXT NOT NULL,
  product_id INTEGER NOT NULL,
  handle TEXT, url TEXT, title TEXT, norm_title TEXT,
  brand TEXT, model TEXT, color TEXT, hardware TEXT, leather TEXT, size TEXT,
  category TEXT,
  first_seen TEXT, last_seen TEXT,
  published_at TEXT, created_at TEXT,
  current_price REAL, currency TEXT DEFAULT 'PHP',
  status TEXT DEFAULT 'active',      -- active|reserved|sold_out|missing|sold|relisted
  initial_sold_out INTEGER DEFAULT 0,  -- already sold-out when first scraped (unknown sold date)
  missing_streak INTEGER DEFAULT 0,
  first_missing_date TEXT,
  sold_date TEXT, sold_price REAL, days_to_sell INTEGER,
  image_src TEXT, image_hash TEXT,
  fingerprint TEXT,
  relisted_to INTEGER,               -- pk of newer listing if matched as re-upload
  UNIQUE(site, product_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
  date TEXT, site TEXT, product_id INTEGER,
  price REAL, available INTEGER, title TEXT,
  PRIMARY KEY (date, site, product_id)
);
CREATE TABLE IF NOT EXISTS price_changes (
  site TEXT, product_id INTEGER, date TEXT,
  old_price REAL, new_price REAL, pct REAL
);
CREATE TABLE IF NOT EXISTS runs (
  date TEXT, site TEXT, products_seen INTEGER, ok INTEGER, error TEXT,
  PRIMARY KEY (date, site)
);
CREATE INDEX IF NOT EXISTS idx_products_site ON products(site);
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status);
CREATE INDEX IF NOT EXISTS idx_snapshots_site_date ON snapshots(site, date);
"""

# ── Normalization ───────────────────────────────────────────────────────────

BRAND_ALIASES = {
    "hermes": "Hermès", "hermès": "Hermès",
    "chanel": "Chanel",
    "louis vuitton": "Louis Vuitton", "lv": "Louis Vuitton",
    "christian dior": "Dior", "dior": "Dior",
    "yves saint laurent": "Saint Laurent", "saint laurent": "Saint Laurent",
    "ysl": "Saint Laurent",
    "goyard": "Goyard", "gucci": "Gucci", "prada": "Prada",
    "celine": "Celine", "céline": "Celine", "loewe": "Loewe",
    "fendi": "Fendi", "bottega veneta": "Bottega Veneta", "bottega": "Bottega Veneta",
    "balenciaga": "Balenciaga", "givenchy": "Givenchy", "valentino": "Valentino",
    "valentino garavani": "Valentino", "moynat": "Moynat", "delvaux": "Delvaux",
    "miu miu": "Miu Miu", "burberry": "Burberry", "mcm": "MCM",
    "salvatore ferragamo": "Ferragamo", "ferragamo": "Ferragamo",
    "alexander mcqueen": "Alexander McQueen", "the row": "The Row",
    "loro piana": "Loro Piana", "alaia": "Alaïa", "alaïa": "Alaïa",
}
KNOWN_BRANDS = sorted(set(BRAND_ALIASES.keys()), key=len, reverse=True)


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def norm_title(title):
    t = strip_accents(html.unescape(title or "")).lower()
    t = re.sub(r"\b(hold|sold|reserved|brand new|preloved|pre-loved|pre-owned|like new)\b", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def normalize_brand(raw):
    if not raw:
        return None
    key = strip_accents(raw).lower().strip()
    return BRAND_ALIASES.get(key)


def detect_brand(product, brand_source):
    """Brand from vendor / tags / title depending on site config."""
    vendor = product.get("vendor") or ""
    b = normalize_brand(vendor)
    if brand_source == "vendor" and b:
        return b
    if brand_source == "tags":
        for tag in product.get("tags") or []:
            b2 = normalize_brand(tag)
            if b2:
                return b2
        if b:
            return b
    # title scan (fallback for all modes)
    nt = " " + norm_title(product.get("title")) + " "
    for cand in KNOWN_BRANDS:
        if f" {strip_accents(cand)} " in nt or nt.startswith(strip_accents(cand) + " "):
            return BRAND_ALIASES[cand]
    # LV special-case: "lv" token
    if re.search(r"\blv\b", nt):
        return "Louis Vuitton"
    if b:
        return b
    return "Other"

# ── Attribute extraction ────────────────────────────────────────────────────

HARDWARE_PATTERNS = [
    (r"\b(aged gold|brushed gold)\b", "Aged Gold"),
    (r"\b(rose gold|rghw)\b", "Rose Gold"),
    (r"\b(champagne gold|chghw)\b", "Champagne Gold"),
    (r"\b(ghw|gold hardware|gold hw|24k)\b", "Gold"),
    (r"\b(phw|palladium)\b", "Palladium"),
    (r"\b(shw|silver hardware|silver hw)\b", "Silver"),
    (r"\b(bghw|brushed gold)\b", "Brushed Gold"),
    (r"\b(ruthenium)\b", "Ruthenium"),
    (r"\b(so black|black hardware)\b", "Black"),
    (r"\b(aghw|antique gold)\b", "Antique Gold"),
]
LEATHERS = [
    "togo", "epsom", "clemence", "swift", "chevre", "ostrich", "barenia",
    "box calf", "boxcalf", "evercolor", "evergrain", "negonda", "taurillon",
    "novillo", "tadelakt", "sombrero", "fjord", "vache", "doblis",
    "crocodile", "croc", "alligator", "lizard", "shiny niloticus", "matte alligator",
    "caviar", "lambskin", "lamb skin", "calfskin", "calf skin", "goatskin",
    "patent", "tweed", "denim", "canvas", "monogram canvas", "damier",
    "epi", "empreinte", "vernis", "suede", "velvet", "satin", "sequin",
    "python", "raffia", "wicker", "toile",
]
LEATHERS = sorted(LEATHERS, key=len, reverse=True)

COLORS = [
    "noir", "black", "blanc", "white", "etoupe", "etain", "gold", "craie",
    "gris", "grey", "gray", "beige", "biscuit", "trench", "nata", "chai",
    "rouge casaque", "rouge h", "rouge grenat", "rouge", "red", "framboise",
    "rose sakura", "rose lipstick", "rose azalee", "rose", "pink", "fuchsia",
    "bleu nuit", "bleu indigo", "bleu zanzibar", "bleu jean", "blue jean",
    "bleu", "blue", "navy", "celeste", "turquoise", "vert", "green", "menthe",
    "anis", "olive", "kaki", "khaki", "brown", "marron", "havane", "ebene",
    "chocolat", "chocolate", "tan", "camel", "caramel", "cognac", "natural",
    "sable", "yellow", "jaune", "lime", "orange", "feu", "apricot", "abricot",
    "purple", "violet", "anemone", "lavender", "lilac", "mauve", "raisin",
    "burgundy", "bordeaux", "wine", "silver", "metallic", "iridescent",
    "ivory", "cream", "multicolor", "tri-color", "tricolor", "bi-color",
    "bicolor", "ombre", "so pink", "vert criquet", "barenia faubourg",
]
COLORS = sorted(COLORS, key=len, reverse=True)

SIZE_PATTERNS = [
    (r"\b(?:birkin|kelly|constance|bolide|lindy|evelyne|picotin|b|k|c)\s*-?\s*(15|18|20|22|24|25|26|28|29|30|31|32|33|35|40|45)\b", None),
    (r"\b(mini|nano|micro|small|medium|jumbo|maxi|large|pm|mm|gm|tpm|bb)\b", None),
    (r"\bsize\s*([0-9]{2}(?:\.[0-9])?)\b", None),
]


def extract_attrs(title, body_text=""):
    """Return dict(color, hardware, leather, size) best-effort from text."""
    text = norm_title(title) + " " + norm_title(body_text[:600])
    out = {"color": None, "hardware": None, "leather": None, "size": None}
    for pat, label in HARDWARE_PATTERNS:
        if re.search(pat, text):
            out["hardware"] = label
            break
    for lea in LEATHERS:
        if re.search(r"\b" + re.escape(lea) + r"\b", text):
            out["leather"] = lea.title()
            break
    for col in COLORS:
        if re.search(r"\b" + re.escape(col) + r"\b", text):
            out["color"] = col.title()
            break
    sizes = []
    m = re.search(SIZE_PATTERNS[0][0], text)
    if m:
        sizes.append(m.group(1))
    m = re.search(SIZE_PATTERNS[1][0], text)
    if m:
        sizes.append(m.group(1))
    m = re.search(SIZE_PATTERNS[2][0], text)
    if m:
        sizes.append(m.group(1))
    out["size"] = "/".join(dict.fromkeys(sizes)) or None
    return out


MODEL_KEYWORDS = [
    "birkin", "kelly pochette", "mini kelly", "kelly cut", "kelly danse",
    "kelly", "constance", "lindy", "evelyne", "garden party", "herbag",
    "picotin", "bolide", "halzan", "roulis", "verrou", "jypsiere", "della cavalleria",
    "geta", "24/24", "haut a courroies", "hac",
    "classic flap", "2.55", "reissue", "boy bag", "boy", "chanel 19", "coco handle",
    "wallet on chain", "woc", "gabrielle", "deauville", "vanity", "trendy cc",
    "pearl crush", "my perfect mini", "heart bag", "kelly to go", "kelly togo",
    "neverfull", "speedy", "alma", "capucines", "onthego", "on the go",
    "pochette metis", "keepall", "noe", "twist", "petit sac plat", "multi pochette",
    "lady dior", "saddle", "diorama", "book tote", "caro", "30 montaigne",
    "marmont", "jackie", "dionysus", "horsebit", "bamboo",
    "saigon", "puzzle", "hammock", "flamenco",
    "saint louis", "anjou", "artois", "rouette", "saigon",
    "peekaboo", "baguette", "first", "le cagole", "city bag",
    "galleria", "cleo", "re-edition", "cassette", "jodie", "arco",
    "sunshine", "loop", "triomphe", "luggage", "belt bag", "box bag",
]
MODEL_KEYWORDS = sorted(MODEL_KEYWORDS, key=len, reverse=True)


def detect_model(title):
    nt = norm_title(title)
    nt_sp = " " + nt + " "
    for kw in MODEL_KEYWORDS:
        k = norm_title(kw)
        if f" {k} " in nt_sp or nt.startswith(k + " ") or nt.endswith(" " + k) or nt == k:
            return kw.title()
    return None


CATEGORY_RULES = [
    (r"\b(necklace|bracelet|ring|earring|brooch|bangle|pendant|jewel|diamond|sapphire|emerald)\b", "Jewelry"),
    (r"\b(watch|rolex|datejust|daytona|submariner|patek|audemars)\b", "Watches"),
    (r"\b(scarf|twilly|shawl|carre|stole|bandeau)\b", "Scarves"),
    (r"\b(shoe|sandal|pump|loafer|sneaker|heel|mule|flat|espadrille|boot|oran)\b", "Shoes"),
    (r"\b(wallet|card holder|cardholder|coin purse|key holder|key pouch|cles|passport|agenda|bearn|calvi)\b", "SLG"),
    (r"\b(belt)\b", "Accessories"),
    (r"\b(charm|rodeo|twilly|keychain|bag charm)\b", "Accessories"),
]


def detect_category(title, product_type=""):
    text = norm_title(title) + " " + norm_title(product_type or "")
    for pat, cat in CATEGORY_RULES:
        if re.search(pat, text):
            return cat
    return "Bags"


def is_reserved(title):
    t = (title or "").lower()
    return bool(re.match(r"^\s*(hold|reserved)\b", t)) or "| hold" in t


def strip_html(s):
    return re.sub(r"<[^>]+>", " ", html.unescape(s or ""))


def fingerprint(norm_t, image_hash, handle):
    base = norm_t or handle or ""
    return f"{base}::{image_hash or ''}"

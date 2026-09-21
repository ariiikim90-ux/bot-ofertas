#!/usr/bin/env python3
"""
Bot de ofertas de Hardgamers -> Discord (webhook).

Uso:
  python bot.py                      # corrida normal (necesita DISCORD_WEBHOOK_URL)
  python bot.py --dry-run            # muestra las alertas en consola, no envía nada
  python bot.py --html-file x.html   # usa un HTML guardado en vez de descargar (pruebas)
  python bot.py --inspect            # muestra qué estructura de datos encontró (para depurar)
"""
import argparse
import html as htmllib
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import requests

BASE = "https://www.hardgamers.com.ar"
HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "state.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

NAME_KEYS = ("name", "title", "displayName", "productName", "description")
PRICE_KEYS = ("price", "currentPrice", "finalPrice", "salePrice", "minPrice",
              "lowestPrice", "amount", "priceValue")
OLD_KEYS = ("oldPrice", "previousPrice", "regularPrice", "listPrice",
            "originalPrice", "prevPrice", "priceBefore", "normalPrice",
            "beforePrice", "lastPrice")
DISC_KEYS = ("discount", "discountPercentage", "discountPercent", "percentage",
             "off", "discountPct")
STORE_KEYS = ("store", "storeName", "shop", "seller", "storeId")
URL_KEYS = ("url", "link", "href", "productUrl", "permalink")
ID_KEYS = ("id", "_id", "productId", "slug", "uid")
IMG_KEYS = ("image", "img", "thumbnail", "imageUrl", "picture", "photo")


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def to_number(v):
    """Convierte 68000, '68000', '$68.000,50' o '$ 68.000' a float."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = re.sub(r"[^\d.,]", "", v)
        if not s:
            return None
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
        elif "." in s and re.search(r"\.\d{3}(\.|$)", s):
            s = s.replace(".", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def fmt_price(v):
    return "$" + f"{v:,.0f}".replace(",", ".")


# --------------------------------------------------------------------------
# Descarga y extracción de datos
# --------------------------------------------------------------------------
def fetch(url, session):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            print(f"  HTTP {r.status_code} en {url}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  error de red ({e}) en {url}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return None


def _scan_objects(text):
    """Busca objetos JSON sueltos dentro de un texto (Next.js streaming, etc.)."""
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        j = text.find("{", i)
        if j == -1:
            break
        try:
            obj, end = dec.raw_decode(text, j)
            yield obj
            i = end
        except ValueError:
            i = j + 1


def extract_json_blobs(page):
    blobs = []
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", page, flags=re.S | re.I)
    for body in scripts:
        body = body.strip()
        if not body:
            continue
        parsed = None
        if body.startswith(("{", "[")):
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
        if parsed is not None:
            blobs.append(parsed)
            continue
        if re.search(r'price', body, re.I):
            text = body
            if '\\"' in text:
                text = text.replace('\\"', '"').replace("\\\\", "\\")
            blobs.extend(_scan_objects(text))
    return blobs


def walk(node, out):
    """Recorre el JSON y junta todo dict que parezca un producto."""
    if isinstance(node, dict):
        name = first(node, NAME_KEYS)
        price = to_number(first(node, PRICE_KEYS))
        if isinstance(name, str) and len(name) >= 6 and price and price > 0:
            out.append(node)
        for v in node.values():
            walk(v, out)
    elif isinstance(node, list):
        for v in node:
            walk(v, out)


def normalize(node):
    name = htmllib.unescape(str(first(node, NAME_KEYS))).strip()
    price = to_number(first(node, PRICE_KEYS))
    old = to_number(first(node, OLD_KEYS))
    disc = to_number(first(node, DISC_KEYS))
    if disc is not None:
        if 0 < disc <= 1:
            disc *= 100
        if disc <= 0 or disc >= 100:
            disc = None
    if old and old > price:
        calc = (old - price) / old * 100
        disc = disc if disc and abs(disc - calc) < 3 else calc
    elif disc and not old:
        old = price / (1 - disc / 100)
    else:
        old, disc = None, None

    pid = first(node, ID_KEYS)
    url = first(node, URL_KEYS)
    if isinstance(url, str):
        url = url if url.startswith("http") else BASE + (url if url.startswith("/") else "/" + url)
    elif pid and isinstance(pid, str) and ":" in pid:
        url = f"{BASE}/product/{pid}"
    store = first(node, STORE_KEYS)
    if isinstance(store, dict):
        store = first(store, ("name", "title", "id"))
    img = first(node, IMG_KEYS)
    if isinstance(img, dict):
        img = first(img, URL_KEYS)
    if isinstance(img, list) and img:
        img = img[0]
    if not isinstance(img, str) or not img.startswith("http"):
        img = None
    return {
        "id": str(pid) if pid else (url or name),
        "name": name, "price": price, "old_price": old, "discount": disc,
        "store": str(store) if store else None, "url": url, "image": img,
    }


def parse_html_fallback(page):
    """Último recurso: leer links /product/ y precios del HTML visible."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    soup = BeautifulSoup(page, "html.parser")
    items = []
    for a in soup.select('a[href*="/product/"]'):
        text = " ".join(a.stripped_strings)
        m = re.findall(r"\$\s?[\d.]+(?:,\d+)?", text)
        if not m:
            continue
        name = re.split(r"\$", text)[0].strip()
        if len(name) < 6:
            continue
        href = a["href"]
        items.append({
            "id": href.split("/product/")[-1], "name": name,
            "price": to_number(m[0]), "old_price": to_number(m[1]) if len(m) > 1 else None,
            "discount": None, "store": None,
            "url": href if href.startswith("http") else BASE + href, "image": None,
        })
    return items


def parse_cards(page):
    """Lee las tarjetas de producto que Hardgamers arma en el HTML (<article class="One-Bit-Product">)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page, "html.parser")
    items = []
    for art in soup.select("article.One-Bit-Product"):
        a = art.select_one('a[href*="/product/"]')
        name_el = art.select_one(".product-name")
        price_el = art.select_one('[itemprop="price"]')
        if not (a and name_el and price_el):
            continue
        price = to_number(price_el.get("content")) or to_number(price_el.get_text())
        if not price:
            continue
        href = a["href"]
        prev_el = art.select_one(".previous-price")
        old = to_number(prev_el.get_text()) if prev_el else None
        off_el = art.select_one(".offer")
        disc = to_number(off_el.get_text()) if off_el else None
        if old and old > price:
            disc = (old - price) / old * 100
        elif disc and 0 < disc < 100:
            old = price / (1 - disc / 100)
        else:
            old, disc = None, None
        store_el = art.select_one(".store")
        img_el = art.select_one("img.img")
        img = img_el.get("src") if img_el else None
        items.append({
            "id": href.split("/product/")[-1].strip(),
            "name": " ".join(name_el.get_text().split()),
            "price": price, "old_price": old, "discount": disc,
            "store": " ".join(store_el.get_text().split()) if store_el else None,
            "url": href if href.startswith("http") else BASE + href,
            "image": img if img and img.startswith("http") else None,
        })
    return items


def parse_page(page, inspect=False):
    cards = parse_cards(page)
    if cards:
        if inspect:
            print(f"  tarjetas de producto leídas: {len(cards)}")
        return cards
    blobs = extract_json_blobs(page)
    nodes = []
    for b in blobs:
        walk(b, nodes)
    if inspect:
        print(f"  bloques JSON: {len(blobs)} | posibles productos: {len(nodes)}")
        for n in nodes[:3]:
            print("  ejemplo de claves:", sorted(n.keys())[:25])
    products = [normalize(n) for n in nodes]
    if not products:
        products = parse_html_fallback(page)
        if inspect:
            print(f"  fallback HTML: {len(products)} productos")
    return [p for p in products if p["price"]]


# --------------------------------------------------------------------------
# Clasificación de productos según lo que te interesa
# --------------------------------------------------------------------------
def ram_info(name):
    up = name.upper()
    if not re.search(r"\bDDR\s?5\b", up):
        return None
    m = re.search(r"(\d+)\s*X\s*(\d+)\s*GB", up)
    cap = int(m.group(1)) * int(m.group(2)) if m else None
    if cap is None:
        m = re.search(r"\b(\d{1,3})\s*GB", up)
        cap = int(m.group(1)) if m else None
    if cap not in (8, 16):
        return None
    sodimm = bool(re.search(r"SO-?DIMM|NOTEBOOK|LAPTOP", up))
    return {"cat": "ram", "group": f"DDR5 {cap}GB{' notebook' if sodimm else ''}",
            "label": f"RAM DDR5 {cap}GB"}


def gpu_info(name):
    up = name.upper()
    if not re.search(r"\b(RX|RTX|RADEON|GEFORCE|PLACA)\b", up):
        return None
    m = re.search(r"\b(8|12|16)\s*G(?:B)?\b", up)
    vram = int(m.group(1)) if m else None
    if re.search(r"\b9060\s*XT\b", up):
        model = "RX 9060 XT"
    elif re.search(r"\b9070\b", up):
        # la 9070 GRE suele traer palabras en el medio ("9070 PRIME GRE 12G")
        if re.search(r"\bGRE\b", up) or vram == 12:
            model = "RX 9070 GRE"
        elif re.search(r"\bXT\b", up):
            model = "RX 9070 XT"
        else:
            model = "RX 9070"
    elif re.search(r"\b5060\s*TI\b", up):
        model = "RTX 5060 Ti"
    elif re.search(r"\b5070\b(?!\s*TI)", up):
        model = "RTX 5070"
    else:
        return None
    defaults = {"RX 9070": 16, "RX 9070 XT": 16, "RX 9070 GRE": 12, "RTX 5070": 12}
    vram = vram or defaults.get(model)
    group = model + (f" {vram}GB" if vram else "")
    return {"cat": "gpu", "group": group, "label": f"GPU {model}"}


PSU_BRANDS = ("corsair", "thermaltake", "gigabyte", "cooler master", "coolermaster")
FAN_BRANDS = PSU_BRANDS + ("id-cooling", "id cooling", "idcooling")


def psu_info(name):
    low = name.lower()
    if not any(b in low for b in PSU_BRANDS):
        return None
    if not re.search(r"\bfuente\b|\bpsu\b|power supply|80\s*plus", low):
        return None
    return {"cat": "psu", "group": "psu", "label": "Fuente de alimentación"}


def fan_info(name):
    low = name.lower()
    if not any(b in low for b in FAN_BRANDS):
        return None
    if not re.search(r"\b(120|140)\s*mm\b", low):
        return None
    if not re.search(r"\bfan\b|ventilador|cooler", low):
        return None
    if re.search(r"\baio\b|liquid|water|refrigeraci[oó]n l[ií]quida|\bkit\b", low):
        return None
    return {"cat": "fan", "group": "fan", "label": "Cooler / fan 120-140mm"}


COMPONENT_WORDS = re.compile(
    r"memoria|\bram\b|ddr[345]|placa de video|\bgpu\b|\brtx\b|\brx\s?\d|geforce|radeon|"
    r"procesador|ryzen|core i[3579]|motherboard|\bmother\b|\bssd\b|\bnvme\b|disco|"
    r"fuente|\bpsu\b|gabinete|cooler|ventilador|\bfan\b|water cooling|refrigeraci", re.I)


def classify(p):
    for fn in (ram_info, gpu_info, psu_info, fan_info):
        info = fn(p["name"])
        if info:
            return info
    if re.search(r"\bDDR\s?[234]\b", p["name"], re.I):
        return None  # RAM vieja (DDR2/3/4): no interesa
    if COMPONENT_WORDS.search(p["name"]):
        return {"cat": "other", "group": "other", "label": "Componente"}
    return None


# --------------------------------------------------------------------------
# Lógica de alertas
# --------------------------------------------------------------------------
def build_medians(items):
    groups = {}
    for p in items:
        if p["cat"] in ("ram", "gpu"):
            groups.setdefault(p["group"], []).append(p["price"])
    return {g: (statistics.median(v), len(v)) for g, v in groups.items()}


def evaluate(p, medians, cfg, prev):
    """Devuelve (lista_de_motivos, ahorro_pct) o ([], None) si no es alerta."""
    reasons = []
    cat = p["cat"]
    min_disc = cfg["min_discount_percent"]

    if p["discount"] and p["discount"] >= min_disc:
        reasons.append(f"{p['discount']:.0f}% de descuento sobre su precio anterior")

    med = medians.get(p["group"]) if cat in ("ram", "gpu") else None
    if med and med[1] >= cfg["min_samples_for_median"]:
        ratio = cfg["ram_max_ratio_of_median"] if cat == "ram" else cfg["gpu_max_ratio_of_median"]
        if p["price"] <= med[0] * ratio:
            pct = (1 - p["price"] / med[0]) * 100
            reasons.append(f"{pct:.0f}% por debajo de la mediana del mercado "
                           f"({fmt_price(med[0])}, {med[1]} publicaciones)")

    cap_key = "ram_max_price" if cat == "ram" else None
    if cap_key and cfg.get(cap_key) and p["price"] <= cfg[cap_key]:
        reasons.append(f"precio por debajo de tu tope ({fmt_price(cfg[cap_key])})")

    if prev and prev.get("price") and p["price"] <= prev["price"] * (1 - cfg["drop_since_last_percent"] / 100):
        pct = (1 - p["price"] / prev["price"]) * 100
        reasons.append(f"bajó {pct:.0f}% desde la última revisión ({fmt_price(prev['price'])})")

    return reasons


def best_saving(p, reasons, medians):
    vals = []
    if p["discount"]:
        vals.append(p["discount"])
    m = medians.get(p["group"])
    if m:
        vals.append((1 - p["price"] / m[0]) * 100)
    return max(vals) if vals else 0


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------
COLORS = {"ram": 0x3498DB, "gpu": 0xE74C3C, "psu": 0xF1C40F, "fan": 0x1ABC9C, "other": 0x95A5A6}


def make_embed(a):
    p = a["product"]
    fields = [{"name": "Precio", "value": fmt_price(p["price"]), "inline": True}]
    if p["old_price"]:
        fields.append({"name": "Precio anterior", "value": fmt_price(p["old_price"]), "inline": True})
    if p["store"]:
        fields.append({"name": "Tienda", "value": p["store"], "inline": True})
    fields.append({"name": "Por qué", "value": "\n".join("• " + r for r in a["reasons"])[:1000]})
    embed = {
        "title": p["name"][:250],
        "color": 0xFF6B00 if a["saving"] >= 50 else COLORS.get(p["cat"], 0x95A5A6),
        "author": {"name": a["info"]["label"]},
        "fields": fields,
        "footer": {"text": "Hardgamers"},
    }
    if p["url"]:
        embed["url"] = p["url"]
    if p["image"]:
        embed["thumbnail"] = {"url": p["image"]}
    return embed


def send_discord(webhook, alerts, mention=None):
    for i in range(0, len(alerts), 10):
        chunk = alerts[i:i + 10]
        payload = {"embeds": [make_embed(a) for a in chunk], "username": "Ofertas Hardgamers"}
        if mention and i == 0:
            payload["content"] = mention
        for _ in range(5):
            r = requests.post(webhook, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            r.raise_for_status()
            break
        time.sleep(1)


# --------------------------------------------------------------------------
# Principal
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--html-file", nargs="+")
    args = ap.parse_args()

    cfg = load_json(CONFIG_FILE, {})
    state = load_json(STATE_FILE, {})
    now = int(time.time())

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9"})

    raw = {}
    if args.html_file:
        for fname in args.html_file:
            text = Path(fname).read_text(encoding="utf-8")
            for p in parse_page(text, args.inspect):
                raw[p["id"]] = p
    else:
        for src in cfg["sources"]:
            if isinstance(src, str):
                src = {"url": src, "pages": cfg.get("max_pages_per_source", 3)}
            base_url, max_pages = src["url"], src.get("pages", 3)
            for pg in range(1, max_pages + 1):
                url = base_url if pg == 1 else re.sub(r"([?&])page=\d+", "", base_url) + f"&page={pg}"
                print(f"Descargando {url}")
                text = fetch(url, session)
                if not text:
                    break
                found = parse_page(text, args.inspect)
                new = [p for p in found if p["id"] not in raw]
                if not new:
                    break
                for p in new:
                    raw[p["id"]] = p
                time.sleep(1.5)

    print(f"Productos leídos: {len(raw)}")
    if not raw:
        print("ERROR: no se pudo leer ningún producto. Corré con --inspect y revisá "
              "que las URLs de config.json abran bien en el navegador.", file=sys.stderr)
        return 1

    items = []
    for p in raw.values():
        info = classify(p)
        if info:
            p.update(cat=info["cat"], group=info["group"])
            items.append((p, info))
    medians = build_medians([p for p, _ in items])
    print(f"Productos de interés: {len(items)}")
    for g, (m, n) in sorted(medians.items()):
        print(f"  mediana {g}: {fmt_price(m)} ({n} publicaciones)")

    alerts = []
    for p, info in items:
        key = p["id"]
        prev = state.get(key)
        reasons = evaluate(p, medians, cfg, prev)
        already = prev and prev.get("alerted") and p["price"] >= prev["alerted"] * 0.95
        if reasons and not already:
            alerts.append({"product": p, "info": info, "reasons": reasons,
                           "saving": best_saving(p, reasons, medians)})
        state[key] = {"price": p["price"],
                      "alerted": prev.get("alerted") if prev else None, "seen": now}

    cutoff = now - 30 * 86400
    alerts.sort(key=lambda a: -a["saving"])
    alerts = alerts[:cfg["max_alerts_per_run"]]
    for a in alerts:  # solo se marcan como avisadas las que realmente se envían
        state[a["product"]["id"]]["alerted"] = a["product"]["price"]
    state = {k: v for k, v in state.items() if v.get("seen", 0) >= cutoff}
    print(f"Alertas nuevas: {len(alerts)}")

    if args.dry_run or args.inspect:
        for a in alerts:
            p = a["product"]
            print(f"- [{a['info']['label']}] {p['name']} | {fmt_price(p['price'])} | "
                  f"{p['store']} | {p['url']}\n    " + " / ".join(a["reasons"]))
    elif alerts:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        if not webhook:
            print("Falta la variable DISCORD_WEBHOOK_URL", file=sys.stderr)
            return 1
        send_discord(webhook, alerts, cfg.get("mention"))

    if not args.dry_run and not args.inspect:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

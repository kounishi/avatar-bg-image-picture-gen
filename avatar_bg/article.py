"""ブログ記事の取得と解析（見出し・本文・記事内画像）。"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

CONTENT_SELECTORS = [
    "[itemprop=articleBody]", ".entry-content", ".post-content", ".article-body", ".article__body",
    ".post_content", ".entry-body", ".blog-entry", ".markdown-body", ".note-common-styles__textnote-body",
    "article", "main", "#content", "#main",
]
DROP_SELECTORS = [
    "script", "style", "noscript", "nav", "footer", "aside", "form", "iframe", "svg",
    "header .site-header", ".sns", ".share", ".social", ".related", ".ad", ".ads", ".advertisement",
    ".breadcrumb", ".comments", "#comments", ".toc", "#toc", ".author-box", ".sidebar",
]
SKIP_IMG = re.compile(r"(logo|icon|avatar|emoji|sprite|badge|banner|spacer|pixel|gravatar|1x1|loading)", re.I)


def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
    return s


def _img_src(img, base: str):
    cands = []
    for attr in ("data-src", "data-lazy-src", "data-original", "data-srcset", "srcset", "src"):
        v = img.get(attr)
        if not v or v.startswith("data:"):
            continue
        if "srcset" in attr:
            parts = [p.strip().split(" ") for p in v.split(",") if p.strip()]
            best = max(parts, key=lambda p: int(re.sub(r"\D", "", p[1]) or 0) if len(p) > 1 else 0)
            cands.append(best[0])
        else:
            cands.append(v)
    return urljoin(base, cands[0]) if cands else None


def fetch_article(url: str, img_dir: Path, min_w: int = 400, min_h: int = 250, log=print) -> dict:
    from bs4 import BeautifulSoup
    from PIL import Image

    sess = _session()
    r = sess.get(url, timeout=30)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(prop):
        t = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (t.get("content") or "").strip() if t else ""

    title = meta("og:title") or (soup.title.get_text(strip=True) if soup.title else "")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)
    site = meta("og:site_name") or urlparse(url).netloc
    # サイト名はキャッチコピー付きのことがあるので区切り記号の手前だけを使う
    site = re.split(r"\s+[-|｜–—]\s+|｜", site)[0].strip()[:30] or urlparse(url).netloc
    # 記事タイトル末尾の「 - サイト名」「 | サイト名」を除去
    parts = re.split(r"\s+[-|｜–—]\s+|｜", title)
    if len(parts) > 1 and (site in parts[-1] or parts[-1] in site or len(parts[-1]) <= 20):
        title = re.sub(r"\s*(?:\s[-|｜–—]\s|｜)\s*" + re.escape(parts[-1]) + r"\s*$", "", title).strip() or title
    description = meta("og:description") or meta("description")
    og_image = meta("og:image")

    for sel in DROP_SELECTORS:
        for el in soup.select(sel):
            el.decompose()

    container = None
    for sel in CONTENT_SELECTORS:
        cands = soup.select(sel)
        if cands:
            container = max(cands, key=lambda c: len(c.get_text(" ", strip=True)))
            if len(container.get_text(strip=True)) > 300:
                break
    if container is None:
        container = max(soup.find_all(["div", "section", "body"]),
                        key=lambda d: sum(len(p.get_text(strip=True)) for p in d.find_all("p", recursive=False)),
                        default=soup.body or soup)

    blocks = []
    for el in container.find_all(["h1", "h2", "h3", "h4", "p", "li", "img", "figcaption", "blockquote"]):
        name = el.name
        if name == "img":
            src = _img_src(el, url)
            if src:
                blocks.append({"type": "image", "src": src, "alt": (el.get("alt") or "").strip()})
        elif name in ("h1", "h2", "h3", "h4"):
            t = el.get_text(" ", strip=True)
            if t:
                blocks.append({"type": "heading", "level": int(name[1]), "text": t})
        elif name == "li" and el.find(["p", "li"]):
            continue
        elif name == "p" and el.find_parent("li"):
            continue
        else:
            t = el.get_text(" ", strip=True)
            if t and len(t) > 1:
                blocks.append({"type": "caption" if name == "figcaption" else "para", "text": t})

    # 画像ごとの周辺文脈（直前の見出し・前後の段落・alt・キャプション）
    images, seen = [], set()
    img_dir.mkdir(parents=True, exist_ok=True)
    heading = title
    for i, b in enumerate(blocks):
        if b["type"] == "heading":
            heading = b["text"]
        if b["type"] != "image":
            continue
        src = b["src"]
        if src in seen or SKIP_IMG.search(src) or src.lower().split("?")[0].endswith((".svg", ".ico")):
            continue
        seen.add(src)
        prev = next((x["text"] for x in reversed(blocks[max(0, i - 3):i]) if x["type"] == "para"), "")
        nxt = [x["text"] for x in blocks[i + 1:i + 4] if x["type"] in ("para", "caption")][:2]
        caption = next((x["text"] for x in blocks[i + 1:i + 2] if x["type"] == "caption"), "")
        images.append({"src": src, "alt": b["alt"], "heading": heading, "caption": caption,
                       "context": " ".join([heading, b["alt"], caption, prev, *nxt]), "order": i})
    if og_image and og_image not in seen:
        images.append({"src": urljoin(url, og_image), "alt": "アイキャッチ", "heading": title, "caption": "",
                       "context": f"{title} {description}", "order": -1, "is_og": True})

    from requests.utils import requote_uri

    kept, skipped = [], 0
    for im in images:
        try:
            rr = sess.get(im["src"], timeout=30, headers={"Referer": requote_uri(url)})
            rr.raise_for_status()
            pic = Image.open(io.BytesIO(rr.content))
            pic.load()
        except Exception as e:  # noqa: BLE001
            skipped += 1
            continue
        if pic.width < min_w or pic.height < min_h:
            continue
        if getattr(pic, "is_animated", False):
            pic.seek(0)
        name = "img_" + hashlib.md5(im["src"].encode()).hexdigest()[:10] + ".png"
        dest = img_dir / name
        pic.convert("RGBA" if pic.mode in ("RGBA", "LA", "P") else "RGB").save(dest)
        im.update({"file": str(dest.resolve()), "width": pic.width, "height": pic.height})
        kept.append(im)
    if skipped:
        log(f"  取得できなかった画像: {skipped} 枚")

    return {
        "url": url, "title": title, "site": site, "domain": urlparse(url).netloc,
        "description": description,
        "headings": [b["text"] for b in blocks if b["type"] == "heading"],
        "paragraphs": [b["text"] for b in blocks if b["type"] == "para"],
        "images": kept,
    }

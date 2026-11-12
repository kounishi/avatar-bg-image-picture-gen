"""背景画像の描画（Pillow）。"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = Path("C:/Windows/Fonts")
BOLD_FONTS = ["BIZ-UDGothicB.ttc", "YuGothB.ttc", "meiryob.ttc", "msgothic.ttc"]
REG_FONTS = ["BIZ-UDGothicR.ttc", "YuGothM.ttc", "meiryo.ttc", "msgothic.ttc"]
LINUX_MAC_FONTS = ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                   "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"]

THEMES = {
    "dark": {"bg1": (13, 20, 38), "bg2": (28, 40, 66), "text": (241, 245, 249), "sub": (148, 163, 184),
             "accent": (56, 189, 248), "accent2": (129, 140, 248), "card": (255, 255, 255, 18),
             "badge_text": (13, 20, 38)},
    "light": {"bg1": (248, 250, 252), "bg2": (219, 228, 240), "text": (15, 23, 42), "sub": (71, 85, 105),
              "accent": (2, 132, 199), "accent2": (99, 102, 241), "card": (255, 255, 255, 190),
              "badge_text": (255, 255, 255)},
}

NO_LINE_START = set("、。，．・：；？！）」』】〕＞ーぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ…,.!?)]}%")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\.\-_/%:+#&']*|.", re.S)
_font_cache: dict = {}


def font(size: int, bold: bool = True):
    key = (size, bold)
    if key not in _font_cache:
        for name in (BOLD_FONTS if bold else REG_FONTS):
            p = FONT_DIR / name
            if p.exists():
                _font_cache[key] = ImageFont.truetype(str(p), size)
                break
        else:
            for p in LINUX_MAC_FONTS:
                if Path(p).exists():
                    _font_cache[key] = ImageFont.truetype(p, size)
                    break
            else:
                _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def wrap(draw, text: str, f, max_w: int) -> list:
    lines, cur = [], ""
    for tok in _TOKEN.findall(text):
        if tok == "\n":
            lines.append(cur)
            cur = ""
            continue
        if draw.textlength(cur + tok, font=f) <= max_w or not cur:
            cur += tok
        elif tok[0] in NO_LINE_START:  # 禁則：行頭に来てはいけない文字はぶら下げ
            cur += tok
        else:
            lines.append(cur)
            cur = tok.lstrip()
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text, max_w, max_h, max_size, min_size, max_lines, bold=True, spacing=1.35):
    for size in range(max_size, min_size - 1, -2):
        f = font(size, bold)
        lines = wrap(draw, text, f, max_w)
        h = int(len(lines) * size * spacing)
        if len(lines) <= max_lines and h <= max_h:
            return f, lines, size
    f = font(min_size, bold)
    lines = wrap(draw, text, f, max_w)[:max_lines]
    if lines:
        lines[-1] = lines[-1][:-1] + "…"
    return f, lines, min_size


def gradient(W, H, c1, c2) -> Image.Image:
    base = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        base.putpixel((0, y), tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    return base.resize((W, H), Image.BICUBIC)


def cover(img: Image.Image, W, H) -> Image.Image:
    sc = max(W / img.width, H / img.height)
    im = img.resize((max(W, round(img.width * sc)), max(H, round(img.height * sc))), Image.LANCZOS)
    x, y = (im.width - W) // 2, (im.height - H) // 2
    return im.crop((x, y, x + W, y + H))


def contain(img: Image.Image, W, H) -> Image.Image:
    sc = min(W / img.width, H / img.height)
    return img.resize((max(1, round(img.width * sc)), max(1, round(img.height * sc))), Image.LANCZOS)


def flatten(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


class Renderer:
    def __init__(self, plan: dict):
        self.W, self.H = plan["size"]
        self.s = min(self.W, self.H) / 1080
        self.th = THEMES.get(plan.get("theme", "dark"), THEMES["dark"])
        self.duration = plan["duration"]
        self.total = len(plan["scenes"])
        self.article = plan.get("article") or {}
        bbs = [b for b in (plan.get("avatar_bboxes") or []) if b]
        self.area = self._duo_area(bbs) if len(bbs) > 1 else self._content_area(plan.get("avatar_bbox"))

    def _duo_area(self, bbs):
        """2人掛け合い：左下・右下のアバターの間（中央）を本文エリアにする。"""
        W, H, s = self.W, self.H, self.s
        top, bottom = int(H * 0.09), int(H * 0.88)
        if self.H > self.W:  # 縦動画：アバターより上
            top = int(H * 0.11)
            bottom = min(bottom, max(int(H * 0.35), min(b[1] for b in bbs) - int(30 * s)))
            return (int(60 * s), top, W - int(110 * s), bottom)
        gap = int(40 * s)
        left = [b for b in bbs if (b[0] + b[2]) / 2 < W / 2]
        right = [b for b in bbs if (b[0] + b[2]) / 2 >= W / 2]
        x0 = max([b[2] for b in left], default=int(80 * s) - gap) + gap
        x1 = min([b[0] for b in right], default=W - int(80 * s) + gap) - gap
        return (x0, top, x1, bottom)

    # アバターと重ならない側を本文エリアにする
    def _content_area(self, bbox):
        W, H, s = self.W, self.H, self.s
        m = int(80 * s)
        top, bottom = int(H * 0.09), int(H * 0.88)
        if self.H > self.W:  # 縦動画（ショート）：上部UI・右側ボタン列を避け、アバターより上を使う
            top = int(H * 0.11)
            if bbox:
                bottom = min(bottom, max(int(H * 0.35), bbox[1] - int(30 * s)))
            return (int(60 * s), top, W - int(110 * s), bottom)
        if not bbox:
            return (m, top, W - m, bottom)
        gap = int(40 * s)
        left_space, right_space = bbox[0] - gap - m, W - bbox[2] - gap - m
        if max(left_space, right_space) < W * 0.4:
            return (m, top, W - m, bottom)  # アバターが中央寄り：全面使用
        if left_space >= right_space:
            return (m, top, bbox[0] - gap, bottom)
        return (bbox[2] + gap, top, W - m, bottom)

    def _base(self, bg_image=None) -> Image.Image:
        th = self.th
        if bg_image is not None:
            im = cover(flatten(bg_image), self.W, self.H).filter(ImageFilter.GaussianBlur(int(28 * self.s)))
            dark = Image.new("RGB", im.size, th["bg1"])
            return Image.blend(im, dark, 0.62)
        im = gradient(self.W, self.H, th["bg1"], th["bg2"])
        deco = Image.new("RGBA", im.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(deco)
        x0, y0, x1, _ = self.area
        r = int(420 * self.s)
        cx = x1 - r // 3 if x0 < self.W * 0.3 else x0 + r // 3
        d.ellipse((cx - r, -r, cx + r, r), fill=th["accent"] + (22,))
        d.ellipse((cx - r // 2 - int(200 * self.s), self.H - r // 2, cx + r // 2, self.H + r), fill=th["accent2"] + (16,))
        im = Image.alpha_composite(im.convert("RGBA"), deco.filter(ImageFilter.GaussianBlur(int(40 * self.s))))
        return im.convert("RGB")

    def _footer(self, im: Image.Image, scene: dict):
        d = ImageDraw.Draw(im, "RGBA")
        s, th = self.s, self.th
        h = max(4, int(6 * s))
        d.rectangle((0, self.H - h, self.W, self.H), fill=th["sub"] + (60,))
        d.rectangle((0, self.H - h, int(self.W * scene["end"] / max(self.duration, 0.01)), self.H), fill=th["accent"])
        src = self.article.get("domain")
        if src:
            # ショートは下部が説明文で隠れるので上部に表示
            y = int(self.H * 0.075) if self.H > self.W else self.H - h - int(44 * s)
            d.text((self.area[0], y), f"出典: {src}", font=font(int(22 * s), False), fill=th["sub"])

    def render(self, scene: dict, custom: Path = None) -> Image.Image:
        if custom:
            return cover(flatten(Image.open(custom)), self.W, self.H)
        t = scene["type"]
        if t == "title":
            return self.title(scene)
        if t == "article_image" and scene.get("image"):
            return self.image_scene(scene)
        return self.slide(scene)

    def title(self, scene):
        bg = Image.open(scene["image"]) if scene.get("image") else None
        im = self._base(bg)
        d = ImageDraw.Draw(im, "RGBA")
        s, th = self.s, self.th
        x0, y0, x1, y1 = self.area
        w = x1 - x0
        label = self.article.get("site") or "記事解説"
        lf = font(int(30 * s), True)
        while len(label) > 1 and d.textlength(label, font=lf) > w - int(40 * s):  # 幅に収まるよう切り詰め
            label = label[:-2] + "…"
        f, lines, size = fit_text(d, scene["title"], w, int((y1 - y0) * 0.6), int(92 * s), int(46 * s), 4)
        lh = int(size * 1.3)
        block = int(60 * s) + len(lines) * lh
        y = y0 + ((y1 - y0) - block) // 2
        d.rounded_rectangle((x0, y, x0 + int(d.textlength(label, font=lf)) + int(40 * s), y + int(48 * s)),
                            radius=int(24 * s), fill=th["accent"])
        d.text((x0 + int(20 * s), y + int(8 * s)), label, font=lf, fill=th["badge_text"])
        y += int(84 * s)
        for ln in lines:
            d.text((x0, y), ln, font=f, fill=th["text"])
            y += lh
        d.rectangle((x0, y + int(16 * s), x0 + int(160 * s), y + int(26 * s)), fill=th["accent"])
        self._footer(im, scene)
        return im

    def slide(self, scene):
        im = self._base()
        d = ImageDraw.Draw(im, "RGBA")
        s, th = self.s, self.th
        x0, y0, x1, y1 = self.area
        w, hgt = x1 - x0, y1 - y0
        bullets = scene.get("bullets") or []
        # 見出しラベル（シーン番号）
        lf = font(int(24 * s), True)
        label = f"{scene['index']:02d} / {self.total:02d}"
        if scene.get("section") and scene["section"] != scene["title"]:
            label += f"   {scene['section']}"
        d.text((x0, y0), label, font=lf, fill=th["accent"])
        # タイトル
        title_h = int(hgt * (0.32 if bullets else 0.7))
        f, lines, size = fit_text(d, scene["title"], w, title_h, int((72 if bullets else 84) * s), int(40 * s),
                                  3 if bullets else 4)
        lh = int(size * 1.3)
        y = y0 + int(50 * s)
        if not bullets:
            y = y0 + (hgt - len(lines) * lh) // 2
        for ln in lines:
            d.text((x0, y), ln, font=f, fill=th["text"])
            y += lh
        d.rectangle((x0, y + int(10 * s), x0 + int(120 * s), y + int(18 * s)), fill=th["accent"])
        y += int(56 * s)
        if bullets:
            self._bullets(d, bullets, x0, y, w, y1 - y)
        self._footer(im, scene)
        return im

    def _bullets(self, d, bullets, x0, y, w, avail, max_size=42):
        s, th = self.s, self.th
        n = len(bullets)
        gap = int(22 * s)
        pad = int(22 * s)
        badge = int(46 * s)
        text_w = w - badge - pad * 3
        for size in range(int(max_size * s), int(22 * s) - 1, -2):
            f = font(size, False)
            wrapped = [wrap(d, b, f, text_w)[:4] for b in bullets]
            lh = int(size * 1.4)
            heights = [max(badge, len(ls) * lh) + pad * 2 for ls in wrapped]
            if sum(heights) + gap * (n - 1) <= avail:
                break
        total = sum(heights) + gap * (n - 1)
        y += max(0, (avail - total) // 3)
        for k, (ls, h) in enumerate(zip(wrapped, heights)):
            d.rounded_rectangle((x0, y, x0 + w, y + h), radius=int(18 * s), fill=th["card"])
            cy = y + h // 2
            bx = x0 + pad
            d.ellipse((bx, cy - badge // 2, bx + badge, cy + badge // 2), fill=th["accent"])
            num = str(k + 1)
            nf = font(int(26 * s), True)
            d.text((bx + (badge - d.textlength(num, font=nf)) / 2, cy - int(16 * s)), num, font=nf,
                   fill=th["badge_text"])
            ty = cy - len(ls) * lh // 2 + int((lh - size) * 0.3)
            for ln in ls:
                d.text((bx + badge + pad, ty), ln, font=f, fill=th["text"])
                ty += lh
            y += h + gap

    def image_scene(self, scene):
        src = Image.open(scene["image"])
        im = self._base(src)
        d = ImageDraw.Draw(im, "RGBA")
        s, th = self.s, self.th
        x0, y0, x1, y1 = self.area
        w = x1 - x0
        bullets = scene.get("bullets") or []
        # タイトル（上部）
        d.text((x0, y0), f"{scene['index']:02d} / {self.total:02d}", font=font(int(24 * s), True), fill=th["accent"])
        f, lines, size = fit_text(d, scene["title"], w, int(150 * s), int(60 * s), int(36 * s), 2)
        lh = int(size * 1.3)
        y = y0 + int(46 * s)
        for ln in lines:
            d.text((x0, y), ln, font=f, fill=th["text"])
            y += lh
        d.rectangle((x0, y + int(8 * s), x0 + int(120 * s), y + int(16 * s)), fill=th["accent"])
        top = y + int(44 * s)
        # 画像と箇条書きの配置（横長エリアなら左右、縦長なら上下）
        gap = int(36 * s)
        rw, rh = w, y1 - top
        # 縦動画や、2人掛け合いの中央エリアのように幅が狭い場合は上下配置
        if bullets and self.W > self.H and rw / max(rh, 1) > 1.3 and rw > self.W * 0.6:
            iw, ih = int(rw * 0.56), rh
            bx, by, bw, bh = x0 + iw + gap, top, rw - iw - gap, rh
        elif bullets:
            iw, ih = rw, int(rh * 0.55)
            bx, by, bw, bh = x0, top + ih + gap, rw, rh - ih - gap
        else:
            iw, ih = rw, rh
        pic = contain(flatten(src), iw, ih)
        px, py = x0 + (iw - pic.width) // 2, top + (ih - pic.height) // 2
        shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((px + 8, py + 14, px + pic.width + 8, py + pic.height + 14),
                                                 radius=int(16 * s), fill=(0, 0, 0, 150))
        im = Image.alpha_composite(im.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(int(16 * s))))
        mask = Image.new("L", pic.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, pic.width, pic.height), radius=int(16 * s), fill=255)
        im.paste(pic, (px, py), mask)
        im = im.convert("RGB")
        d = ImageDraw.Draw(im, "RGBA")
        if bullets:
            self._bullets(d, bullets, bx, by, bw, bh, max_size=36)
        self._footer(im, scene)
        return im

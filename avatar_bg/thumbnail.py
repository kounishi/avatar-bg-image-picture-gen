"""YouTube サムネイル画像の作成（チャンネル名・記事タイトル・アバター）。"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .slides import THEMES, cover, flatten, font, gradient, wrap

CH_COLOR = (220, 38, 38)        # チャンネル名の帯（赤）
TAG_COLOR = (255, 214, 0)       # 【】タグの帯・サブタイトル（黄）
TEXT = (255, 255, 255)
STROKE = (10, 18, 40)


def split_title(title: str):
    """記事タイトルを【タグ】・メイン・サブに分ける。"""
    title = re.sub(r"\s+", " ", title.replace("　", " ")).strip()
    tag = ""
    m = re.match(r"^\s*[【\[]([^】\]]{1,24})[】\]]\s*", title)
    if m:
        tag, title = m.group(1), title[m.end():]
    main, sub = title, ""
    n = len(title)
    if n > 28:  # 長いタイトルは、4割以上進んだ最初の空白でメインとサブに分ける
        for sp in re.finditer(r" ", title):
            if sp.start() >= n * 0.4 and n - sp.end() >= 6:
                main, sub = title[:sp.start()], title[sp.end():]
                break
        if not sub:  # 空白がなければ「？」「！」の後で分ける
            for q in re.finditer(r"[？！?!]", title):
                if q.end() >= n * 0.25 and n - q.end() >= 8:
                    main, sub = title[:q.end()], title[q.end():]
                    break
    return tag, main.strip(), sub.strip()


def _phrases(text: str) -> list:
    out, pos = [], 0
    for m in re.finditer(r"(?<=[）」』】\])、。・〜～!！?？ ])|(?=[（「『【\[(])", text):
        if m.start() > pos:
            out.append(text[pos:m.start()])
            pos = m.start()
    out.append(text[pos:])
    return [p for p in out if p]


def wrap_phrases(draw, text: str, f, max_w: int) -> list:
    """文節（括弧・中黒などの区切り）単位で折り返す。長すぎる文節は文字単位で折り返す。"""
    lines, cur = [], ""
    for ph in _phrases(text):
        if draw.textlength((cur + ph).rstrip(), font=f) <= max_w:
            cur += ph
            continue
        if cur.strip():
            lines.append(cur.rstrip())
            cur = ""
        if draw.textlength(ph.rstrip(), font=f) <= max_w:
            cur = ph.lstrip()
        else:
            parts = wrap(draw, ph.strip(), f, max_w)
            lines += parts[:-1]
            cur = parts[-1] if parts else ""
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def _fit(draw, text, max_w, max_h, max_size, min_size, max_lines, spacing=1.22):
    for size in range(max_size, min_size - 1, -2):
        f = font(size, True)
        lines = wrap_phrases(draw, text, f, max_w)
        if (len(lines) <= max_lines and len(lines) * size * spacing <= max_h
                and max(draw.textlength(ln, font=f) for ln in lines) <= max_w * 1.02):  # ぶら下げ文字のはみ出しも含めて判定
            return f, lines, size
    f = font(min_size, True)
    lines = wrap_phrases(draw, text, f, max_w)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return f, lines, min_size


EXPRESSIONS = [  # (番号, キー, 表示名)
    (1, "standard", "標準の笑顔"),
    (2, "open", "口を大きく開けた笑顔"),
    (3, "closed", "口を閉じた笑顔"),
]


def _alpha_bbox(im: Image.Image):
    return im.split()[-1].point(lambda v: 255 if v > 40 else 0).getbbox()


def _grab_frames(info: dict, times: list, work: Path) -> list:
    from .media import decoder_args, run

    frames = []
    for k, tt in enumerate(times):
        png = work / f"cand_{k:03d}.png"
        r = run(["ffmpeg", "-v", "error", "-y", "-ss", f"{tt:.3f}", *decoder_args(info), "-i", info["path"],
                 "-frames:v", "1", "-vf", "format=rgba", str(png)])
        if r.returncode == 0 and png.exists():
            frames.append((tt, Image.open(png).convert("RGBA")))
    return frames


def _analyze_faces(frames: list, union, cand: list = None, full_body: bool = False) -> dict:
    """顔まわりを位置合わせして比較し、標準・口を大きく開けた・口を閉じたコマの番号を返す。

    - 頭の揺れを平行移動で補正してから、コマ間で頻繁に変化する塊を「口」とみなす
      （まばたきはたまにしか起きないので口と区別できる。細い輪郭線の揺れは収縮処理で消す）
    - 口の範囲で肌色でない画素の割合を「口の開き具合」とする
    - 口以外（目など）が他のコマと大きく違うコマ（まばたき・うつむき）は候補から外す
    """
    import numpy as np

    sc = 0.4
    # 比較する顔まわりの範囲：上半身の素材は上 60%、全身の素材（頭が大きいキャラクターなど）は上 75%
    face = (union[0], union[1], union[2], union[1] + int((union[3] - union[1]) * (0.75 if full_body else 0.6)))
    A = []
    for f in frames:
        c = f.crop(face)
        c = c.resize((max(16, round(c.width * sc)), max(16, round(c.height * sc))))
        A.append(np.asarray(c.convert("RGB"), np.float32))
    L = [a.mean(2) for a in A]
    ref = np.median(np.stack(L), 0)
    h, w = ref.shape
    R = max(2, min(12, w // 20, h // 20))
    core = ref[R:h - R, R:w - R]

    def best_shift(g):
        best = (float("inf"), 0, 0)
        for dy in range(-R, R + 1):
            for dx in range(-R, R + 1):
                err = float(np.abs(g[R + dy:h - R + dy, R + dx:w - R + dx] - core).mean())
                if err < best[0]:
                    best = (err, dy, dx)
        return best

    sh = [best_shift(g) for g in L]
    AL = np.stack([L[i][R + dy:h - R + dy, R + dx:w - R + dx] for i, (_, dy, dx) in enumerate(sh)])
    CL = np.stack([A[i][R + dy:h - R + dy, R + dx:w - R + dx] for i, (_, dy, dx) in enumerate(sh)])
    med = np.median(AL, 0)
    dev = np.abs(AL - med)
    n = len(frames)

    def medoid(idx):
        return min(idx, key=lambda i: sum(float(np.abs(AL[i] - AL[j]).mean()) for j in idx))

    cand = list(range(n)) if cand is None else cand  # 選んでよいコマ（口の位置の推定には全コマを使う）
    result = {"standard": medoid(cand), "open": None, "closed": None}
    frac = (dev > 30).mean(0)
    mc = np.median(CL, 0)
    # まず両目（瞳）を探し、その下だけで口を探す（目の表情がよく変わるキャラクターでも口と取り違えない）
    eyes = _find_pupils(med, mc)
    if eyes:
        r2 = _mouth_by_eyes(eyes, frac, mc, CL, dev, cand)
        if r2:
            ok, opn, area, pink = r2
            result["standard"] = medoid(ok)
            result["open"] = max(ok, key=lambda i: (opn[i], area[i]))
            # 口の中が見えず、線が細いコマ
            result["closed"] = min(ok, key=lambda i: (round(float(pink[i]) * 50), opn[i], area[i]))
            return result
    # 瞳が見つからない場合：よく変化する画素の塊を口とみなす
    frac = (dev > 30).mean(0)
    fim = Image.fromarray((frac * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(5)).filter(ImageFilter.BoxBlur(3))
    fa = np.asarray(fim, np.float32) / 255
    # 肌色（明るく赤みのある色）に囲まれた範囲だけで口を探す（髪の輪郭の揺れを拾わないため）
    mc = np.median(CL, 0)
    r_, g_, b_ = mc[..., 0], mc[..., 1], mc[..., 2]
    skin_m = (r_ > 170) & (r_ >= g_) & (g_ >= b_ - 10) & (r_ - b_ > 30) & (g_ > 100)
    if skin_m.mean() > 0.03:
        k = max(5, (min(skin_m.shape) // 12) | 1)
        inside = np.asarray(Image.fromarray((skin_m * 255).astype(np.uint8)).filter(ImageFilter.BoxBlur(k // 2)),
                            np.float32) / 255 > 0.6
        fa = fa * inside
        skin = np.median(mc[skin_m], 0)
    else:
        skin = None
    if fa.max() < 0.12:  # 口がほとんど動かない素材
        return result
    my, mx = np.unravel_index(int(fa.argmax()), fa.shape)
    ry, rx = max(4, int(fa.shape[0] * 0.08)), max(6, int(fa.shape[1] * 0.12))
    y0, y1 = max(0, my - ry), min(fa.shape[0], my + ry)
    x0, x1 = max(0, mx - rx), min(fa.shape[1], mx + rx)
    ys, xs = np.nonzero(fa[y0:y1, x0:x1] > fa.max() * 0.35)
    if len(ys):
        y0, y1, x0, x1 = y0 + ys.min(), y0 + ys.max() + 1, x0 + xs.min(), x0 + xs.max() + 1
    bh = y1 - y0  # 大きく開けた口が収まるよう少し広げる
    y0, y1 = max(0, y0 - max(2, bh // 4)), min(fa.shape[0], y1 + max(3, bh // 2))
    if skin is None:  # 肌色が見つからない場合は口のすぐ上下の色
        ring = np.concatenate([mc[max(0, y0 - 4):y0, x0:x1].reshape(-1, 3), mc[y1:y1 + 4, x0:x1].reshape(-1, 3)])
        skin = np.median(ring, 0) if len(ring) else np.median(mc.reshape(-1, 3), 0)
    mouth = CL[:, y0:y1, x0:x1]
    nonskin = np.sqrt(((mouth - skin) ** 2).sum(-1)) > 50
    # 開き具合＝口の縦の厚み（閉じた口は細い線、歯を見せた口は少し厚く、大きく開けた口は最も厚い）
    col_h = nonskin.sum(1)  # (コマ, 列)
    opn = np.array([float(np.percentile(c[c > 0], 90)) if (c > 0).any() else 0.0 for c in col_h])
    area = nonskin.mean((1, 2))
    # 口以外の顔（目など）の崩れ
    mask = np.ones(med.shape, bool)
    mask[max(0, y0 - ry):y1 + ry, max(0, x0 - rx):x1 + rx] = False
    other = np.array([float(dev[i][mask].mean()) for i in range(n)])
    ok = [i for i in cand if other[i] <= np.median(other) * 1.35] or cand
    result["standard"] = medoid(ok)
    result["open"] = max(ok, key=lambda i: (opn[i], area[i]))
    result["closed"] = min(ok, key=lambda i: (opn[i], area[i], other[i]))
    return result


def _skin_mask(mc):
    r_, g_, b_ = mc[..., 0], mc[..., 1], mc[..., 2]
    return (r_ > 195) & (g_ > 150) & (b_ > 105) & (r_ >= g_) & (g_ >= b_ - 10) & (r_ - b_ > 8)


def _components(mask) -> list:
    """2値画像の連結成分 [(面積, 中心y, 中心x, (y0, y1, x0, x1))]。"""
    import numpy as np

    h, w = mask.shape
    seen = np.zeros_like(mask, bool)
    out = []
    for sy, sx in zip(*np.nonzero(mask)):
        if seen[sy, sx]:
            continue
        stack, pts = [(sy, sx)], []
        seen[sy, sx] = True
        while stack:
            y, x = stack.pop()
            pts.append((y, x))
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        ys = [p[0] for p in pts]
        xs = [p[1] for p in pts]
        out.append((len(pts), sum(ys) / len(ys), sum(xs) / len(xs), (min(ys), max(ys) + 1, min(xs), max(xs) + 1)))
    return out


def _find_pupils(med, mc):
    """中央値画像から、肌に囲まれた暗い丸い塊（瞳）の左右ペアを探す。返り値 ((y, x), (y, x)) か None。"""
    import numpy as np

    h, w = med.shape
    skin = _skin_mask(mc)
    if skin.mean() < 0.02:
        return None
    k = max(9, (min(h, w) // 4) | 1)
    near_skin = np.asarray(Image.fromarray((skin * 255).astype(np.uint8)).filter(ImageFilter.BoxBlur(k // 2)),
                           np.float32) / 255
    # 細い輪郭線を消して、ある程度の大きさの暗い塊だけ残す
    lmax = np.asarray(Image.fromarray(med.astype(np.uint8)).filter(ImageFilter.MaxFilter(3)), np.float32)
    dark = lmax < 95
    def roundish(c):  # 瞳らしい丸い塊（細長い線や、輪郭の切れ端を除く）
        bh_, bw_ = c[3][1] - c[3][0], c[3][3] - c[3][2]
        return 0.5 <= bh_ / max(1, bw_) <= 2.2 and c[0] / max(1, bh_ * bw_) >= 0.45 and min(bh_, bw_) >= 3

    comps = [c for c in _components(dark)
             if c[0] >= 6 and roundish(c) and near_skin[int(c[1]), int(c[2])] > 0.15]
    best = None
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            a, b = comps[i], comps[j]
            dy, dx = abs(a[1] - b[1]), abs(a[2] - b[2])
            if dx < w * 0.08 or dx > w * 0.6 or dy > max(3, dx * 0.2):
                continue
            if max(a[0], b[0]) > min(a[0], b[0]) * 3:
                continue
            ha, hb = a[3][1] - a[3][0], b[3][1] - b[3][0]
            wa, wb = a[3][3] - a[3][2], b[3][3] - b[3][2]
            if ha > dx * 0.9 or hb > dx * 0.9 or wa > dx * 0.8 or wb > dx * 0.8:  # 目にしては大きすぎる（ゴーグル等）
                continue
            sc_ = min(a[0], b[0]) * (near_skin[int(a[1]), int(a[2])] + near_skin[int(b[1]), int(b[2])])
            if best is None or sc_ > best[0]:
                best = (sc_, a, b)
    if not best:
        return None
    a, b = sorted(best[1:], key=lambda c: c[2])
    return (a[1], a[2]), (b[1], b[2])


def _mouth_by_eyes(eyes, frac, mc, CL, dev, cand):
    """両目の位置から口の範囲を決め、各コマの開き具合と、目が普段どおりのコマ（候補）を返す。"""
    import numpy as np

    (ly, lx), (ry_, rx_) = eyes
    h, w = frac.shape
    ed = rx_ - lx
    ey, cx = (ly + ry_) / 2, (lx + rx_) / 2
    # 口は「目の少し下〜あごの上」の中央（瞳の間隔 ed を基準にした顔の比率）
    wy0, wy1 = int(ey + ed * 0.3), int(min(h, ey + ed * 1.1))
    wx0, wx1 = int(max(0, cx - ed * 0.4)), int(min(w, cx + ed * 0.4))
    if wy1 - wy0 < 3 or wx1 - wx0 < 3:
        return None
    win = np.asarray(Image.fromarray((frac * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(3))
                     .filter(ImageFilter.BoxBlur(1)), np.float32)[wy0:wy1, wx0:wx1] / 255
    if win.max() < 0.08:
        return None
    my, mx = np.unravel_index(int(win.argmax()), win.shape)
    fw = frac[wy0:wy1, wx0:wx1]
    # よく変化する画素の塊のうち、最も変化の大きい点を含む（または最も近い）塊を口の範囲にする
    comps = _components(fw > max(0.08, float(fw.max()) * 0.3))
    if not comps:
        return None
    blob = min(comps, key=lambda c: (0 if c[3][0] <= my < c[3][1] and c[3][2] <= mx < c[3][3] else 1,
                                     (c[1] - my) ** 2 + (c[2] - mx) ** 2))
    y0, y1 = wy0 + blob[3][0], wy0 + blob[3][1]
    x0, x1 = wx0 + blob[3][2], wx0 + blob[3][3]
    bh = max(2, y1 - y0)
    y0, y1 = max(0, y0 - max(1, bh // 4)), min(h, y1 + max(2, bh // 3))
    x0, x1 = max(0, x0 - 1), min(w, x1 + 1)
    skin_m = _skin_mask(mc)
    ring = mc[max(0, y0 - 6):min(h, y1 + 6), max(0, x0 - 6):min(w, x1 + 6)]
    rmask = skin_m[max(0, y0 - 6):min(h, y1 + 6), max(0, x0 - 6):min(w, x1 + 6)]
    skin = np.median(ring[rmask], 0) if rmask.any() else np.median(mc[skin_m], 0)
    mouth = CL[:, y0:y1, x0:x1]
    nonskin = np.sqrt(((mouth - skin) ** 2).sum(-1)) > 50
    col_h = nonskin.sum(1)
    opn = np.array([float(np.percentile(c[c > 0], 90)) if (c > 0).any() else 0.0 for c in col_h])
    area = nonskin.mean((1, 2))
    # 口の中（舌・口内）のピンク〜赤：閉じた口は濃い色の線だけなので、ここが少ないほど「閉じた口」
    r_, g_, b_ = mouth[..., 0], mouth[..., 1], mouth[..., 2]
    pink = (nonskin & (r_ > 140) & (r_ - g_ > 35) & (r_ - b_ > 20)).mean((1, 2))
    # 目の崩れ（まばたき・半目・ウインク）：瞳まわりの差分が普段より大きいコマは外す
    ew, eh = max(2, int(ed * 0.28)), max(2, int(ed * 0.25))
    eye_dev = np.zeros(len(CL))
    for (yy, xx) in eyes:
        sl = (slice(max(0, int(yy) - eh), int(yy) + eh), slice(max(0, int(xx) - ew), int(xx) + ew))
        eye_dev += np.array([float(dev[i][sl].mean()) for i in range(len(CL))])
    thr = max(np.median(eye_dev) * 1.5, np.median(eye_dev) + 4)
    ok = [i for i in cand if eye_dev[i] <= thr] or cand
    return ok, opn, area, pink


def extract_avatars(info: dict, t, work: Path) -> dict:
    """透過アバター動画から表情3パターンのコマを選んで切り出す。

    返り値 {"standard"|"open"|"closed": (キャラクター部分の RGBA 画像, 下端が切れているか, 秒)}。
    t を指定するとその前後約2秒、None なら動画全体から候補を取り出す。
    """
    if not (info.get("has_video") and info.get("has_alpha")):
        return {}
    work.mkdir(parents=True, exist_ok=True)
    dur = info["duration"]
    times = [dur * (k + 0.5) / 48 for k in range(48)]
    local = []
    if t is not None:  # 指定時刻の前後約2秒から選ぶ（口の位置は動画全体のコマで推定）
        local = sorted({round(min(max(0.0, t + d / 6), dur - 0.1), 3) for d in range(-12, 13)})
        times = local + times[::2]
    grabbed = _grab_frames(info, times, work)
    if not grabbed:
        return {}
    times, frames = [g[0] for g in grabbed], [g[1] for g in grabbed]
    cand = [i for i, tt in enumerate(times) if tt in local] if local else None
    union = None
    for im in frames:
        bb = _alpha_bbox(im)
        if bb:
            union = bb if union is None else (min(union[0], bb[0]), min(union[1], bb[1]),
                                              max(union[2], bb[2]), max(union[3], bb[3]))
    if not union or (union[2] - union[0]) * (union[3] - union[1]) > frames[0].width * frames[0].height * 0.97:
        return {}
    full_body = union[3] < frames[0].height - 2  # 下端が画面下で切れていない＝全身
    picks = _analyze_faces(frames, union, cand or None, full_body)
    out = {}
    for key in ("standard", "open", "closed"):
        i = picks[key] if picks.get(key) is not None else picks["standard"]
        bb = _alpha_bbox(frames[i])
        out[key] = (frames[i].crop(bb), bb[3] >= frames[i].height - 2, times[i])  # 下端が画面下で切れているか
    return out


def _outlined(av: Image.Image, width: int) -> Image.Image:
    """キャラクターの周囲に白フチと影をつける。"""
    pad = width * 3
    big = Image.new("RGBA", (av.width + pad * 2, av.height + pad * 2), (0, 0, 0, 0))
    big.paste(av, (pad, pad), av)
    alpha = big.split()[-1]
    edge = alpha.filter(ImageFilter.MaxFilter(width * 2 + 1)).filter(ImageFilter.GaussianBlur(1))
    shadow = alpha.filter(ImageFilter.MaxFilter(width * 2 + 1)).filter(ImageFilter.GaussianBlur(width * 1.5))
    out = Image.new("RGBA", big.size, (0, 0, 0, 0))
    sh = Image.new("RGBA", big.size, (0, 0, 0, 255))
    sh.putalpha(shadow.point(lambda v: int(v * 0.55)))
    out = Image.alpha_composite(out, ImageChops.offset(sh, width, width))
    white = Image.new("RGBA", big.size, (255, 255, 255, 255))
    white.putalpha(edge)
    out = Image.alpha_composite(out, white)
    return Image.alpha_composite(out, big), pad


def _background(W, H, bg_file, theme, text_side_w):
    th = THEMES.get(theme, THEMES["dark"])
    if bg_file and Path(bg_file).exists():
        im = cover(flatten(Image.open(bg_file)), W, H).filter(ImageFilter.GaussianBlur(max(2, W // 400)))
    else:
        im = gradient(W, H, th["bg1"], th["bg2"])
    im = im.convert("RGBA")
    # 文字側を暗くして読みやすくする
    ov = Image.new("L", (256, 1))
    for x in range(256):
        t = x / 255
        ov.putpixel((x, 0), int(225 * (1 - t) ** 1.2 + 60))
    if H > W:  # 縦：上側を暗く
        ov = ov.rotate(-90, expand=True)
    mask = ov.resize((W, H), Image.BICUBIC)
    dark = Image.new("RGBA", (W, H), (8, 12, 28, 255))
    dark.putalpha(mask)
    im = Image.alpha_composite(im, dark)
    # アクセントの斜めライン
    d = ImageDraw.Draw(im, "RGBA")
    s = min(W, H) / 720
    if H <= W:
        d.polygon([(0, H - int(26 * s)), (text_side_w, H - int(26 * s)), (text_side_w - int(30 * s), H), (0, H)],
                  fill=TAG_COLOR + (255,))
    return im


def _pill(d, x, y, text, f, fill, color, pad_x, pad_y, radius):
    tw = d.textlength(text, font=f)
    asc, desc = f.getmetrics()
    h = asc + desc + pad_y * 2
    d.rounded_rectangle((x, y, x + tw + pad_x * 2, y + h), radius=radius, fill=fill)
    d.text((x + pad_x, y + pad_y), text, font=f, fill=color)
    return int(tw + pad_x * 2), h


def render_thumbnail(size, channel: str, title: str, avatar, bg_file=None, theme="dark") -> Image.Image:
    W, H = size
    vertical = H > W
    s = min(W, H) / 720
    tag, main, sub = split_title(title)

    # アバターの配置
    av_img, av_pos = None, None
    duo = isinstance(avatar, list)
    placed = []  # 2人の場合 [(フチつき画像, 位置)]
    if duo:
        avs = [x for x in avatar if x]
        th_ = int(H * (0.42 if vertical else 0.74))
        sizes = [th_ / a_[0].height for a_ in avs]
        total_w = sum(a_[0].width * k for a_, k in zip(avs, sizes)) * 0.9
        lim = W * (0.92 if vertical else 0.46)
        if total_w > lim:
            sizes = [k * lim / total_w for k in sizes]
        ims = [a_[0].resize((max(1, round(a_[0].width * k)), max(1, round(a_[0].height * k))), Image.LANCZOS)
               for a_, k in zip(avs, sizes)]
        widths = [im_.width for im_ in ims]
        span = sum(widths) - int(min(widths) * 0.1 * (len(ims) - 1))  # 少し重ねて並べる
        x = (W - span) // 2 if vertical else W - span - int(18 * s)
        boxes = []
        for (im_, (_, cut)) in zip(ims, [(a_[0], a_[1]) for a_ in avs]):
            y = H - im_.height if cut else H - im_.height - int(16 * s)
            o, pad = _outlined(im_, max(4, int(6 * s)))
            placed.append((o, (x - pad, y - pad)))
            boxes.append((x, y, x + im_.width, y + im_.height))
            x += im_.width - int(min(widths) * 0.1)
        av_box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), H) if boxes else None
        avatar = None
    if avatar:
        av, cut_bottom = avatar
        target_h = int(H * (0.50 if vertical else 0.96))
        sc = target_h / av.height
        sc = min(sc, (W * (0.95 if vertical else 0.40)) / av.width)
        av = av.resize((max(1, round(av.width * sc)), max(1, round(av.height * sc))), Image.LANCZOS)
        av_img, pad = _outlined(av, max(4, int(7 * s)))
        y = H - av.height if cut_bottom else H - av.height - int(16 * s)
        x = (W - av.width) // 2 if vertical else W - av.width - int(18 * s)
        av_pos = (x - pad, y - pad)
        av_box = (x, y, x + av.width, y + av.height)
    elif not duo:
        av_box = None

    margin = int(44 * s)
    if vertical:
        x0, x1 = int(70 * s), W - int(70 * s)
        y0 = int(H * 0.07)
        y1 = (av_box[1] - int(30 * s)) if av_box else int(H * 0.9)
    else:
        x0 = margin
        x1 = (av_box[0] - int(10 * s)) if av_box else W - margin
        y0, y1 = int(36 * s), H - int(56 * s)
    tw = x1 - x0

    im = _background(W, H, bg_file, theme, x1)
    d = ImageDraw.Draw(im, "RGBA")

    # チャンネル名
    y = y0
    cf = font(int(34 * s), True)
    if channel:
        _, h = _pill(d, x0, y, channel, cf, CH_COLOR, TEXT, int(22 * s), int(10 * s), int(10 * s))
        y += h + int(18 * s)
    # 【タグ】
    if tag:
        tf = font(int(40 * s), True)
        while len(tag) > 2 and d.textlength(tag, font=tf) > tw - int(40 * s):
            tf = font(tf.size - 2, True)
        _, h = _pill(d, x0, y, tag, tf, TAG_COLOR, STROKE, int(18 * s), int(8 * s), int(4 * s))
        y += h + int(18 * s)

    # メイン・サブタイトル（白フチ文字）
    avail = y1 - y
    sw = max(3, int(6 * s))
    sub_h = int(avail * 0.3) if sub else 0
    mf, mlines, msize = _fit(d, main, tw, avail - sub_h - (int(14 * s) if sub else 0),
                             int((120 if vertical else 92) * s), int(38 * s), 5 if vertical else 4)
    mlh = int(msize * 1.22)
    blocks_h = len(mlines) * mlh
    if sub:
        sf, slines, ssize = _fit(d, sub, tw, avail - blocks_h - int(14 * s), min(int(msize * 0.72), int(64 * s)),
                                 int(28 * s), 3)
        slh = int(ssize * 1.25)
        blocks_h += int(14 * s) + len(slines) * slh
    y += max(0, (avail - blocks_h) // 2) if vertical else max(0, (avail - blocks_h) // 3)
    for ln in mlines:
        d.text((x0, y), ln, font=mf, fill=TEXT, stroke_width=sw, stroke_fill=STROKE)
        y += mlh
    if sub:
        y += int(14 * s)
        for ln in slines:
            d.text((x0, y), ln, font=sf, fill=TAG_COLOR, stroke_width=max(3, int(sw * 0.8)), stroke_fill=STROKE)
            y += slh

    if av_img is not None:
        im.alpha_composite(av_img, (max(-av_img.width, av_pos[0]), av_pos[1]))
    for o, pos in reversed(placed):  # 左（最初の話者）を手前に
        im.alpha_composite(o, (max(0, pos[0]), max(0, pos[1])))
    return im.convert("RGB")


def pick_background(plan: dict, custom_dir: Path):
    """背景：custom/thumbnail_bg.* → plan の thumbnail.background → 記事のアイキャッチ → 最初の記事内画像。"""
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = custom_dir / f"thumbnail_bg.{ext}"
        if p.exists():
            return str(p)
    t = plan.get("thumbnail") or {}
    if t.get("background"):
        return t["background"]
    imgs = (plan.get("article") or {}).get("images") or []
    og = [i for i in imgs if i.get("is_og")]
    land = [i for i in imgs if not i.get("is_og") and i.get("width", 0) >= i.get("height", 1)]
    for cand in (og, land, imgs):
        if cand:
            return cand[0]["file"]
    return None


def make_thumbnails(plan: dict, out: Path, log=print) -> list:
    """表情3パターンのサムネイルを、横（1280x720）と、ショート版がある場合は縦（1080x1920）で出力する。

    thumbnail_1〜3.jpg と、選択中のパターン（plan の thumbnail.pick、既定 1）を thumbnail.jpg / .png として
    出力し、比較用に thumbnail_choices.jpg も作る。
    """
    t = plan.get("thumbnail") or {}
    channel = t.get("channel", "")
    title = t.get("title") or (plan.get("article") or {}).get("title") or (plan["scenes"][0]["title"] if plan["scenes"] else "")
    at = t.get("avatar_time")
    infos = [av["info"] for av in plan["avatars"]] if plan.get("avatars") else [plan["media"]]
    sets = []
    for k, info in enumerate(infos):
        with tempfile.TemporaryDirectory() as tmp:
            sets.append(extract_avatars(info, None if at is None else float(at), Path(tmp)))
    sets_ok = [x for x in sets if x]
    avatars = sets_ok[0] if sets_ok else {}
    if not sets_ok and plan["media"].get("has_video"):
        log("  ※ 透過アバターを切り出せなかったため、サムネイルはアバターなしで作成します")
    for info, x in zip(infos, sets):
        if x:
            log(f"  表情（{Path(info['path']).stem}）: "
                + " / ".join(f"{n}.{label}（{x[k][2]:.1f}秒のコマ）" for n, k, label in EXPRESSIONS))
    bg = pick_background(plan, out / "custom")
    outputs = [((1280, 720), out)]
    for v in plan.get("variants", []):
        outputs.append(((1080, 1920), out / v["name"]))
    for size, d in outputs:
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("thumbnail*.*"):
            old.unlink()
        ims = []
        for n, key, _ in (EXPRESSIONS if avatars else EXPRESSIONS[:1]):
            if len(sets_ok) > 1:  # 2人掛け合い：2人とも同じ表情パターン
                av_arg = [x[key][:2] for x in sets_ok]
            else:
                av = avatars.get(key)
                av_arg = av[:2] if av else None
            im = render_thumbnail(size, channel, title, av_arg, bg, plan.get("theme", "dark"))
            im.save(d / f"thumbnail_{n}.png")
            im.save(d / f"thumbnail_{n}.jpg", quality=92, optimize=True)
            ims.append(im)
        if len(ims) > 1:
            _choices_sheet(ims, size).save(d / "thumbnail_choices.jpg", quality=88)
    pick = int(t.get("pick") or 1) if avatars else 1
    return select_thumbnail(out, plan, pick, log=None)


def select_thumbnail(out: Path, plan: dict, pick: int, log=print) -> list:
    """thumbnail_<pick> を thumbnail.jpg / .png にコピーする（横・ショート両方）。"""
    import shutil

    done = []
    for d in [out] + [out / v["name"] for v in plan.get("variants", [])]:
        src = d / f"thumbnail_{pick}.jpg"
        if not src.exists():
            continue
        shutil.copyfile(src, d / "thumbnail.jpg")
        shutil.copyfile(d / f"thumbnail_{pick}.png", d / "thumbnail.png")
        done.append(d / "thumbnail.jpg")
    if log:
        for f in done:
            log(f"サムネイル（{pick}. {expression_label(pick)}）: {f}")
    return done


def expression_label(n: int) -> str:
    return next((lb for k, _, lb in EXPRESSIONS if k == n), "")


def _choices_sheet(ims: list, size) -> Image.Image:
    """3パターンを番号つきで並べた比較画像。"""
    W, H = size
    tw = 640 if W > H else 360
    th = round(H * tw / W)
    gap, top = 16, 56
    sheet = Image.new("RGB", (gap + (tw + gap) * len(ims), top + th + gap), (30, 30, 36))
    d = ImageDraw.Draw(sheet)
    f = font(26, True)
    for k, (im, (n, _, label)) in enumerate(zip(ims, EXPRESSIONS)):
        x = gap + k * (tw + gap)
        sheet.paste(im.resize((tw, th), Image.LANCZOS), (x, top))
        d.text((x, 14), f"{n}. {label}", font=f, fill=(255, 255, 255))
    return sheet

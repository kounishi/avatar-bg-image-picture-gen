"""シーン分割・背景画像の割り当て。"""
from __future__ import annotations

import re
from fractions import Fraction

from .util import concise, similarity

INF = float("inf")


def segment(sentences: list, target: float, min_sec: float, max_sec: float) -> list:
    """動的計画法で文をシーンにまとめる。返り値は [(i, j), ...]（文 i〜j-1）。"""
    n = len(sentences)

    def seg_cost(i, j):
        if sentences[i]["section"] != sentences[j - 1]["section"]:
            return INF  # シナリオの見出しをまたがない
        d = sentences[j - 1]["end"] - sentences[i]["start"]
        c = ((d - target) / target) ** 2
        if d < min_sec:
            c += 3 + (min_sec - d)
        if d > max_sec and j - i > 1:
            c += 5 + (d - max_sec)
        return c

    def boundary_cost(j):  # 文 j-1 と j の間で切るコスト
        if j >= n:
            return 0.0
        if sentences[j]["section"] != sentences[j - 1]["section"]:
            return 0.0
        return 0.0 if sentences[j]["para"] != sentences[j - 1]["para"] else 0.6

    dp = [INF] * (n + 1)
    prev = [0] * (n + 1)
    dp[0] = 0.0
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            if sentences[j - 1]["end"] - sentences[i]["start"] > max_sec * 3:
                break
            if dp[i] == INF:
                continue
            c = dp[i] + seg_cost(i, j) + boundary_cost(j)
            if c < dp[j]:
                dp[j], prev[j] = c, i
        if dp[j] == INF:  # どうしても分割できない場合の保険
            dp[j], prev[j] = dp[j - 1] + 10, j - 1
    segs, j = [], n
    while j > 0:
        segs.append((prev[j], j))
        j = prev[j]
    return segs[::-1]


def build_plan(sentences, article, media_info, size, opts) -> dict:
    W, H = size
    fps = Fraction(media_info["fps"]) if media_info["has_video"] else Fraction(opts.fps)
    duration = media_info["duration"]
    total_frames = max(1, round(duration * fps))

    segs = segment(sentences, opts.target_sec, opts.min_sec, opts.max_sec)

    # 隙間なく並べる：シーン境界は「前シーン最後の文の終わり」と「次シーン最初の文の始まり」の中間
    bounds = [0]
    for (i, j), (i2, _) in zip(segs, segs[1:]):
        t = (sentences[j - 1]["end"] + sentences[i2]["start"]) / 2
        bounds.append(round(t * fps))
    bounds.append(total_frames)
    for k in range(1, len(bounds)):  # 各シーン最低1フレーム
        bounds[k] = max(bounds[k], bounds[k - 1] + 1)
    bounds[-1] = max(bounds[-1], total_frames)

    scenes = []
    used_titles = set()
    a_title = (article or {}).get("title", "")
    headings = [h for h in (article or {}).get("headings", []) if similarity(h, a_title) < 0.8]
    for k, (i, j) in enumerate(segs):
        sents = sentences[i:j]
        text = "".join(s["text"] for s in sents)
        sec_title = sents[0]["section_title"]
        first_of_section = i == 0 or sentences[i - 1]["section"] != sents[0]["section"]
        title, body = None, [s["text"] for s in sents]
        if k == 0 and opts.title_card and article:
            title = article["title"]  # 冒頭はタイトル画像（記事の見出しを消費しない）
        elif sec_title and (first_of_section or sec_title not in used_titles):
            title = sec_title
        if not title and headings:
            best = max(headings, key=lambda h: similarity(h, text))
            if similarity(best, text) >= 0.12 and best not in used_titles and len(best) <= 40:
                title = best
        dialog = any(s.get("speaker") for s in sents)
        if not title and dialog:
            # 掛け合い：聞き手の質問をタイトルにし、答えを箇条書きにする
            qs = [x for x, t in enumerate(body) if re.search(r"[？?]$", t) and len(concise(t, 99)) >= 8]
            if qs:
                title = concise(body[qs[0]], 40)
                if len(concise(body[qs[0]], 99)) <= 40:
                    body = body[:qs[0]] + body[qs[0] + 1:]
        if dialog:
            rest = [t for t in body if not re.search(r"[？?]$", t) and not _is_aizuchi(t)]
            body = rest or body
        if not title:
            # 挨拶などの短い文は飛ばし、内容のある最初の文をタイトルにする
            k_t = next((x for x, t in enumerate(body) if len(concise(t, 99)) >= 10), 0)
            title = concise(body[k_t], 30)
            if len(concise(body[k_t], 99)) <= 30:  # 長い文はタイトルで切れるので箇条書きにも残す
                body = body[:k_t] + body[k_t + 1:]
        used_titles.add(title)
        bullets = _pick_bullets(body, opts.max_bullets)
        sf, ef = bounds[k], bounds[k + 1]
        scenes.append({
            "index": k + 1,
            "start_frame": sf, "end_frame": ef,
            "start": float(sf / fps), "end": float(ef / fps),
            "type": "slide",
            "title": title,
            "bullets": bullets,
            "section": sec_title,
            "image": None, "image_score": 0.0,
            "narration": text,
        })

    if opts.title_card and scenes and article:
        scenes[0]["type"] = "title"
        scenes[0]["title"] = article["title"]

    _assign_images(scenes, (article or {}).get("images", []), opts)

    counts = {}
    for s in scenes:
        counts[s["type"]] = counts.get(s["type"], 0) + 1
    return {
        "version": 1,
        "size": [W, H],
        "fps": f"{fps.numerator}/{fps.denominator}" if fps.denominator != 1 else str(fps.numerator),
        "duration": duration,
        "total_frames": total_frames,
        "theme": opts.theme,
        "media": media_info,
        "avatar_bbox": None,
        "article": {k: v for k, v in (article or {}).items() if k != "paragraphs"} if article else None,
        "counts": counts,
        "scenes": scenes,
    }


_AIZUCHI = re.compile(r"^(そのとおり|その通り|そうだね|そう|うん|はい|なるほど|ええ|そうなんです|そうですね|いいね|正解)[。！!、]*$")


def _is_aizuchi(t: str) -> bool:
    """「そのとおり。」「そうだね。」などの相づちだけの文。"""
    return bool(_AIZUCHI.match(t.strip()))


def _pick_bullets(body: list, max_n: int) -> list:
    items = [concise(t) for t in body]
    items = [x for x in items if len(x) >= 4]
    if len(items) <= max_n:
        return items
    # 情報量（長さ）の多い文を優先しつつ元の順序を保つ
    keep = sorted(sorted(range(len(items)), key=lambda x: -len(items[x]))[:max_n])
    return [items[x] for x in keep]


def _assign_images(scenes, images, opts):
    """記事内画像をナレーションとの類似度が高いシーンに1枚ずつ割り当てる。"""
    if not images or opts.max_article_images == 0:
        return
    pairs = []
    for im in images:
        for s in scenes:
            if s["type"] == "title" and not im.get("is_og"):
                continue
            score = similarity(im["context"], s["narration"])
            if im.get("is_og"):
                score = 1.0 if s["type"] == "title" else score * 0.5
            pairs.append((score, s["index"], im["file"]))
    pairs.sort(reverse=True)
    used_img, used_scene, n = set(), set(), 0
    for score, idx, f in pairs:
        if f in used_img or idx in used_scene:
            continue
        sc = scenes[idx - 1]
        if sc["type"] == "title":
            sc["image"], sc["image_score"] = f, round(score, 3)
        elif score >= opts.image_threshold and n < opts.max_article_images:
            sc["type"], sc["image"], sc["image_score"] = "article_image", f, round(score, 3)
            n += 1
        else:
            continue
        used_img.add(f)
        used_scene.add(idx)

"""シナリオの各文がアバター音声の何秒に話されているかを求める。"""
from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path

from .media import detect_silences
from .util import norm_chars


def transcribe(media: Path, cache: Path, model: str = "small", log=print) -> list:
    """faster-whisper で単語タイムスタンプ付き文字起こし → [(char, t0, t1), ...]"""
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("model") == model:
            log("  文字起こし: キャッシュを使用")
            return [tuple(x) for x in data["chars"]]
    from faster_whisper import WhisperModel

    log(f"  文字起こし中（faster-whisper {model} / CPU）… 動画の長さによって数分かかります")
    wm = WhisperModel(model, device="cpu", compute_type="int8")
    segments, _ = wm.transcribe(str(media), language="ja", word_timestamps=True, vad_filter=True,
                                condition_on_previous_text=False)
    chars = []
    for seg in segments:
        for w in seg.words or []:
            cs = norm_chars(w.word)
            if not cs:
                continue
            step = (w.end - w.start) / len(cs)
            for i, c in enumerate(cs):
                chars.append((c, w.start + step * i, w.start + step * (i + 1)))
        log(f"    {seg.end:7.1f}s まで処理", end="\r")
    log("")
    cache.write_text(json.dumps({"model": model, "chars": chars}, ensure_ascii=False), encoding="utf-8")
    return chars


def align_with_transcript(sentences: list, chars: list) -> int:
    """各文を文字起こしの探索窓内で照合し start/end を付与。照合できた文の数を返す。"""
    T = "".join(c for c, _, _ in chars)
    cursor = 0
    for s in sentences:
        S = norm_chars(s["text"])
        win_end = min(len(T), cursor + int(2.5 * len(S)) + 60)
        window = T[cursor:win_end]
        sm = SequenceMatcher(None, S, window, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size >= 2]
        s["start"] = s["end"] = None
        if not blocks or sum(b.size for b in blocks) < max(3, 0.35 * len(S)):
            continue
        # 各一致断片から「文の開始位置」「終了位置」を推定し、断片の長さで重み付けした中央値を採る。
        # 「インターチェンジ」のような頻出語が離れた位置に偶然一致しても多数決で除外される。
        i0 = cursor + _wmedian([(b.b - b.a, b.size) for b in blocks])
        i1 = cursor + _wmedian([(b.b + b.size + (len(S) - b.a - b.size), b.size) for b in blocks]) - 1
        i0 = min(max(cursor, i0), len(T) - 1)
        i1 = min(max(i0, i1), len(T) - 1)
        s["start"], s["end"] = chars[i0][1], chars[i1][2]
        cursor = i1 + 1
    _reject_outliers(sentences)
    matched = sum(1 for s in sentences if s["start"] is not None)
    _interpolate(sentences)
    return matched


def _wmedian(pairs: list) -> int:
    pairs = sorted(pairs)
    half, acc = sum(w for _, w in pairs) / 2, 0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def _reject_outliers(sentences: list):
    """話速（秒/文字）が全体の中央値から極端に外れる文や、時刻が逆行する文は未照合扱いにする。"""
    rates = sorted((s["end"] - s["start"]) / max(1, len(norm_chars(s["text"])))
                   for s in sentences if s["start"] is not None)
    if len(rates) < 3:
        return
    med = rates[len(rates) // 2]
    last_end = 0.0
    for s in sentences:
        if s["start"] is None:
            continue
        r = (s["end"] - s["start"]) / max(1, len(norm_chars(s["text"])))
        if r > med * 2.2 or r < med * 0.35 or s["start"] < last_end - 0.5:
            s["start"] = s["end"] = None
            continue
        last_end = s["end"]


def _interpolate(sentences: list):
    """照合できなかった文は前後の照合済み文から文字数比で補間。"""
    n = len(sentences)
    lens = [max(1, len(norm_chars(s["text"]))) for s in sentences]
    i = 0
    while i < n:
        if sentences[i]["start"] is not None:
            i += 1
            continue
        j = i
        while j < n and sentences[j]["start"] is None:
            j += 1
        t0 = sentences[i - 1]["end"] if i > 0 else 0.0
        t1 = sentences[j]["start"] if j < n else None
        if t1 is None:  # 末尾が未照合：直前までの話速で外挿
            done = [k for k in range(i) if sentences[k]["start"] is not None]
            rate = ((sentences[done[-1]]["end"] - sentences[done[0]]["start"]) / sum(lens[done[0]:done[-1] + 1])
                    if done else 0.13)
            t1 = t0 + rate * sum(lens[i:j])
        total = sum(lens[i:j])
        t = t0
        for k in range(i, j):
            d = (t1 - t0) * lens[k] / total
            sentences[k]["start"], sentences[k]["end"] = t, t + d
            t += d
        i = j
    # 単調増加を保証
    last = 0.0
    for s in sentences:
        s["start"] = max(s["start"], last)
        s["end"] = max(s["end"], s["start"] + 0.05)
        last = s["end"]


def align_proportional(sentences: list, media: Path, duration: float):
    """文字起こしが使えない場合：発話区間を文字数比で割り振り、境界を近くの無音に吸着。"""
    sil = detect_silences(media)
    speech_start = sil[0][1] if sil and sil[0][0] <= 0.05 and sil[0][1] else 0.0
    speech_end = sil[-1][0] if sil and sil[-1][1] is None else duration
    lens = [max(1, len(norm_chars(s["text"]))) for s in sentences]
    total, t = sum(lens), speech_start
    span = max(0.1, speech_end - speech_start)
    for s, L in zip(sentences, lens):
        s["start"] = t
        t += span * L / total
        s["end"] = t
    mids = [((a + b) / 2, a, b) for a, b in sil if b is not None]
    for k in range(len(sentences) - 1):
        bd = sentences[k]["end"]
        near = min(mids, key=lambda m: abs(m[0] - bd), default=None)
        if near and abs(near[0] - bd) < 2.0:
            sentences[k]["end"], sentences[k + 1]["start"] = near[1], near[2]
    _interpolate_fix(sentences)


def _interpolate_fix(sentences):
    last = 0.0
    for s in sentences:
        s["start"] = max(s["start"], last)
        s["end"] = max(s["end"], s["start"] + 0.05)
        last = s["end"]


def align(sentences: list, media: Path, duration: float, work: Path, model: str, log=print) -> str:
    if model and model.lower() != "none":
        try:
            chars = transcribe(media, work / f"transcript_{model}.json", model, log)
            if chars:
                n = align_with_transcript(sentences, chars)
                log(f"  シナリオ {len(sentences)} 文中 {n} 文を音声と照合しました")
                if n >= len(sentences) * 0.3:
                    return "whisper"
                log("  照合率が低いため、無音検出による推定に切り替えます")
        except Exception as e:  # noqa: BLE001
            log(f"  文字起こしに失敗しました（{e.__class__.__name__}: {e}）。無音検出で推定します")
    align_proportional(sentences, media, duration)
    return "proportional"

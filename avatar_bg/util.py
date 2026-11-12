"""共通ユーティリティ。"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from fractions import Fraction


def norm_chars(text: str) -> str:
    """照合用に正規化（NFKC・小文字化・記号/空白除去）。"""
    t = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in t if ch.isalnum())


def bigrams(text: str) -> Counter:
    s = norm_chars(text)
    if len(s) < 2:
        return Counter([s]) if s else Counter()
    return Counter(s[i:i + 2] for i in range(len(s) - 1))


def similarity(a: str, b: str) -> float:
    """文字バイグラムのコサイン類似度（日本語でも形態素解析なしで使える）。"""
    ca, cb = bigrams(a), bigrams(b)
    if not ca or not cb:
        return 0.0
    dot = sum(v * cb.get(k, 0) for k, v in ca.items())
    na = math.sqrt(sum(v * v for v in ca.values()))
    nb = math.sqrt(sum(v * v for v in cb.values()))
    return dot / (na * nb)


def fmt_tc(sec: float) -> str:
    """HH:MM:SS.mmm"""
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def fmt_jp(sec: float) -> str:
    """「1分23.4秒」形式"""
    m, s = divmod(sec, 60)
    return f"{int(m)}分{s:04.1f}秒"


def parse_fps(s: str) -> Fraction:
    try:
        f = Fraction(s)
        if f > 0:
            return f.limit_denominator(1001)
    except (ValueError, ZeroDivisionError):
        pass
    return Fraction(30)


SIZE_PRESETS = {
    "youtube": (1920, 1080), "fhd": (1920, 1080), "1080p": (1920, 1080),
    "hd": (1280, 720), "720p": (1280, 720),
    "4k": (3840, 2160), "2160p": (3840, 2160), "1440p": (2560, 1440),
    "shorts": (1080, 1920), "vertical": (1080, 1920),
}


def parse_size(s: str) -> tuple:
    s = s.strip().lower()
    if s in SIZE_PRESETS:
        return SIZE_PRESETS[s]
    m = re.fullmatch(r"(\d+)\s*[x×*]\s*(\d+)", s)
    if not m:
        raise ValueError(f"サイズの指定が不正です: {s}（例: 1920x1080, 4k, shorts）")
    w, h = int(m.group(1)), int(m.group(2))
    return (w - w % 2, h - h % 2)


_FILLERS = re.compile(
    r"^(さて|では|それでは|まず|次に|そして|また|ちなみに|実は|ここで|最後に|続いて|さらに|つまり|なお|まずは|ところで|というわけで|ということで)[、,]?\s*"
)
_ENDINGS = [
    (re.compile(r"(なんです|なのです|んです|のです)(よ|ね|よね)?[。！!？?]*$"), ""),
    (re.compile(r"(と思います|と考えられます|と言えるでしょう|と言えます|でしょう)(ね|よ)?[。！!？?]*$"), ""),
    (re.compile(r"(です|でした)(よ|ね|よね)?[。！!？?]*$"), ""),
    (re.compile(r"[。！!]+$"), ""),
]


def concise(sentence: str, limit: int = 64) -> str:
    """ナレーション文をスライド向けの短い箇条書きに整形する（簡易ルール）。"""
    s = sentence.strip()
    s = _FILLERS.sub("", s)
    for pat, rep in _ENDINGS:
        s2 = pat.sub(rep, s)
        if s2 != s and len(s2) >= 6:
            s = s2
            break
    s = s.strip("、, ")
    if len(s) > limit:
        cut = max(s.rfind("、", 0, limit), s.rfind("，", 0, limit))
        s = (s[:cut] if cut >= limit * 0.5 else s[:limit - 1]) + "…"
    return s

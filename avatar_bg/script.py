"""シナリオテキストの解析。"""
from __future__ import annotations

import re
from pathlib import Path

from .util import norm_chars

HEADING_PATTERNS = [
    re.compile(r"^#{1,6}\s*(.+)$"),               # ## 見出し
    re.compile(r"^【(.+?)】\s*$"),                 # 【導入】
    re.compile(r"^[■□◆◇●▼▶]\s*(.+)$"),           # ■ 見出し
    re.compile(r"^\[(.+?)\]\s*$"),                 # [セクション]
    re.compile(r"^(?:第\s*\d+\s*[章節部]|\d+[\.．、])\s*(.{1,30})$"),  # 1. 見出し（短いもの）
]
SPEAKER = re.compile(r"^(?:ナレーション|ナレーター|アバター|話者|司会|[A-Za-z]{1,10})\s*[:：]\s*")
STAGE = re.compile(r"[（(](?:間|BGM|SE|効果音|ここで.*?|画像.*?|図.*?)[)）]")
SENT_SPLIT = re.compile(r"(?<=[。！？!?])\s*")


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp932", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# 掛け合い台本の話者ラベル：「話者ID,セリフ」「話者：セリフ」「話者「セリフ」」
DIALOG = re.compile(r"^([^\s,，:：「」『』。、！？!?]{1,32})\s*(?:[,，:：]\s*|(?=「))(.+)$")


def _detect_speakers(lines: list) -> dict:
    """2人以上の話者ラベルが大半の行に付いていれば {行番号: (話者, 本文)} を返す。"""
    hits = {}
    for i, ln in enumerate(lines):
        m = DIALOG.match(ln)
        if m:
            body = m.group(2).strip()
            if body.startswith("「") and body.endswith("」"):
                body = body[1:-1]
            hits[i] = (m.group(1), body)
    counts = {}
    for sp, _ in hits.values():
        counts[sp] = counts.get(sp, 0) + 1
    main = [sp for sp, c in counts.items() if c >= 2]
    if len(main) >= 2 and sum(counts[sp] for sp in main) >= len(lines) * 0.6:
        return {i: v for i, v in hits.items() if v[0] in main}
    return {}


def speakers_of(sentences: list) -> list:
    """登場順の話者リスト（掛け合いでなければ空）。"""
    out = []
    for s in sentences:
        sp = s.get("speaker")
        if sp and sp not in out:
            out.append(sp)
    return out


def parse_script(path: Path) -> list:
    """文単位のリストを返す: {text, para, section, section_title, speaker}

    掛け合い台本（「話者ID,セリフ」など）の場合は speaker に話者を入れ、最初の話者（聞き手）が
    話し始めるところを段落の区切りとする（質問と答えがひとまとまりになりやすい）。
    """
    text = read_text(path).replace("\r\n", "\n")
    raw_lines = text.split("\n")
    spk = _detect_speakers([ln.strip() for ln in raw_lines if ln.strip()])
    first_sp = None
    if spk:
        first_sp = spk[min(spk)][0]
    sentences, para, section, section_title = [], 0, 0, ""
    new_para = True
    k_line, prev_sp = -1, None
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            new_para = True
            continue
        k_line += 1
        speaker = None
        if k_line in spk:
            speaker, line = spk[k_line]
            if speaker == first_sp and prev_sp is not None and prev_sp != speaker:
                new_para = True
            prev_sp = speaker
        head = None
        for pat in HEADING_PATTERNS:
            m = pat.match(line)
            if m and len(m.group(1)) <= 40 and not re.search(r"[。！？!?]$", line):
                head = m.group(1).strip()
                break
        if head:
            section += 1
            section_title = head
            new_para = True
            continue
        if new_para and sentences:
            para += 1
        new_para = False
        line = STAGE.sub("", line if speaker else SPEAKER.sub("", line)).strip()
        for s in SENT_SPLIT.split(line):
            s = s.strip()
            if s and norm_chars(s):
                sentences.append({"text": s, "para": para, "section": section, "section_title": section_title,
                                  "speaker": speaker})
    if not sentences:
        raise ValueError("シナリオから文章を読み取れませんでした。")
    return sentences

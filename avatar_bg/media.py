"""ffmpeg / ffprobe まわり。"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .util import parse_fps

ALPHA_PIX_FMTS = ("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya8", "ya16", "pal8")


def _need(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        raise RuntimeError(f"{tool} が見つかりません。ffmpeg をインストールし PATH を通してください。")
    return p


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def probe(path: Path) -> dict:
    _need("ffprobe")
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe に失敗しました: {r.stderr.strip()}")
    info = json.loads(r.stdout)
    fmt = info.get("format", {})
    vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
               and not s.get("disposition", {}).get("attached_pic")), None)
    as_ = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(fmt.get("duration") or (vs or as_ or {}).get("duration") or 0)
    out = {
        "path": str(path.resolve()),
        "duration": duration,
        "has_video": vs is not None,
        "has_audio": as_ is not None,
        "width": None, "height": None, "fps": "30", "has_alpha": False, "vp9_alpha": False,
        "audio_rate": int(as_.get("sample_rate", 48000)) if as_ else 48000,
        "audio_channels": int(as_.get("channels", 2)) if as_ else 2,
    }
    if vs:
        out["width"], out["height"] = int(vs["width"]), int(vs["height"])
        fr = parse_fps(vs.get("r_frame_rate") or vs.get("avg_frame_rate") or "30")
        out["fps"] = f"{fr.numerator}/{fr.denominator}" if fr.denominator != 1 else str(fr.numerator)
        pix = (vs.get("pix_fmt") or "").lower()
        vp9a = str(vs.get("tags", {}).get("alpha_mode", vs.get("tags", {}).get("ALPHA_MODE", ""))) == "1"
        out["vp9_alpha"] = vp9a and vs.get("codec_name") in ("vp8", "vp9")
        out["has_alpha"] = vp9a or any(pix.startswith(p) for p in ALPHA_PIX_FMTS)
    return out


def decoder_args(info: dict) -> list:
    """VP9/VP8 のアルファ付き webm は libvpx デコーダを明示しないとアルファが落ちる。"""
    return ["-c:v", "libvpx-vp9"] if info.get("vp9_alpha") else []


def avatar_layout(info: dict, out_w: int, out_h: int, pos: str = "bottom-right",
                  height: float = 0.8, margin: float = 0.02):
    """アバターの配置を決める。

    キャラクターの見えている部分（アルファの不透明領域）を基準に、
    pos=bottom-right なら右下隅に着くよう配置し、見えている高さが画面高さ×height になるよう縮小する。
    返り値: {"pos", "scale", "x", "y", "w", "h", "bbox"}（x,y,w,h は縮小後の動画全体の配置、bbox は見えている範囲）
    """
    if not (info["has_video"] and info["has_alpha"]):
        return None  # 透過なし・音声のみは compose 側で別処理
    vw, vh = info["width"], info["height"]
    src = avatar_source_bbox(info)
    if src is None or pos == "original":
        sc = min(out_w / vw, out_h / vh)  # 元動画のまま画面に収まるよう中央配置
        x, y = (out_w - vw * sc) / 2, (out_h - vh * sc) / 2
    else:
        bw, bh = src[2] - src[0], src[3] - src[1]
        sc = out_h * height / bh
        sc = min(sc, out_w * (0.9 if out_h > out_w else 0.45) / bw)  # 横幅を取りすぎないよう上限
        m = round(out_w * margin)
        y = out_h - src[3] * sc  # 見えている部分の下端を画面下端に合わせる
        if src[3] < vh * 0.97:   # 全身など下端に余白がある素材は少し浮かせる
            y -= m
        if pos == "bottom-left":
            x = m - src[0] * sc
        elif pos == "center":
            x = out_w / 2 - (src[0] + src[2]) / 2 * sc
        else:
            x = out_w - m - src[2] * sc
    bbox = None
    if src is not None:
        bbox = [round(x + src[0] * sc), round(y + src[1] * sc), round(x + src[2] * sc), round(y + src[3] * sc)]
        bbox = [max(0, bbox[0]), max(0, bbox[1]), min(out_w, bbox[2]), min(out_h, bbox[3])]
    w, h = round(vw * sc / 2) * 2, round(vh * sc / 2) * 2
    return {"pos": pos, "scale": sc, "x": round(x), "y": round(y), "w": w, "h": h, "bbox": bbox}


def avatar_source_bbox(info: dict, samples: int = 6):
    """透過アバターの不透明領域（元動画の座標）を数フレームの和集合で求める。"""
    if not (info["has_video"] and info["has_alpha"]):
        return None
    import numpy as np
    from PIL import Image

    vw, vh, dur = info["width"], info["height"], info["duration"]
    box = None
    with tempfile.TemporaryDirectory() as td:
        for i in range(samples):
            t = dur * (i + 0.5) / samples
            png = Path(td) / f"a{i}.png"
            r = run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", *decoder_args(info), "-i", info["path"],
                     "-frames:v", "1", "-vf", "format=rgba,alphaextract,scale=480:-2", str(png)])
            if r.returncode != 0 or not png.exists():
                continue
            a = np.asarray(Image.open(png).convert("L"))
            ys, xs = np.where(a > 40)
            if len(xs) == 0:
                continue
            sx, sy = vw / a.shape[1], vh / a.shape[0]
            b = (xs.min() * sx, ys.min() * sy, (xs.max() + 1) * sx, (ys.max() + 1) * sy)
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    if box is None:
        return None
    full = (box[2] - box[0]) * (box[3] - box[1]) / (vw * vh)
    if full > 0.97:  # ほぼ全面不透明＝実質的に透過していない
        return None
    return [round(v) for v in box]


def detect_silences(path: Path, noise_db: int = -35, min_dur: float = 0.25) -> list:
    """[(start, end), ...] の無音区間。"""
    r = run(["ffmpeg", "-v", "info", "-nostats", "-i", str(path), "-vn",
             "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        out.append((max(0.0, s), e))
    return out


def avatar_tracks(plan: dict) -> list:
    """合成・XML 出力用のアバター一覧 [(メディア情報, 配置)]。1人の場合は plan["media"] だけ。"""
    if plan.get("avatars"):
        return [(av["info"], av.get("layout")) for av in plan["avatars"]]
    return [(plan["media"], plan.get("avatar_layout"))]


def audio_fingerprint(path, start: float = 0.0, dur: float = 60.0) -> str:
    """音声の一部の md5（複数のアバター動画に同じ会話音声が入っているかの判定用）。"""
    r = run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path), "-map", "0:a:0",
             "-ac", "1", "-ar", "16000", "-f", "md5", "-"])
    return r.stdout.strip() if r.returncode == 0 else ""


def mix_audio(paths: list, dest) -> None:
    """各アバター動画に本人の声だけが入っている場合に、全員の音声を1本に混ぜる。"""
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for p in paths:
        cmd += ["-i", str(p)]
    cmd += ["-filter_complex", "".join(f"[{i}:a:0]" for i in range(len(paths)))
            + f"amix=inputs={len(paths)}:normalize=0:duration=longest[a]", "-map", "[a]", "-c:a", "pcm_s16le",
            str(dest)]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError("音声のミックスに失敗しました: " + r.stderr[-300:])

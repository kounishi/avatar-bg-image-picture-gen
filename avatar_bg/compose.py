"""ffmpeg で 背景画像列 ＋ 透過アバター ＋ 音声 を合成して mp4 を出力。"""
from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path

from .media import avatar_tracks, decoder_args


def compose(plan: dict, out: Path, dest: Path, crf: int = 18, preset: str = "medium", log=print):
    W, H = plan["size"]
    fps = Fraction(plan["fps"])
    m = plan["media"]
    lst = out / "work" / "concat.txt"
    lines = ["ffconcat version 1.0"]
    for s in plan["scenes"]:
        dur = Fraction(s["end_frame"] - s["start_frame"]) / fps
        lines.append(f"file '{Path(s['file']).resolve().as_posix()}'")
        lines.append(f"duration {float(dur):.6f}")
    lines.append(f"file '{Path(plan['scenes'][-1]['file']).resolve().as_posix()}'")
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")

    total = float(Fraction(plan["total_frames"]) / fps)
    bg = f"[0:v]scale={W}:{H},fps={fps.numerator}/{fps.denominator},format=yuv420p,setsar=1[bg]"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats",
           "-f", "concat", "-safe", "0", "-i", str(lst), *decoder_args(m), "-i", m["path"]]
    lay = plan.get("avatar_layout")
    tracks = avatar_tracks(plan)
    audio_map = "1:a?"
    if len(tracks) > 1:  # 2人掛け合い：左下・右下にそれぞれ縮小配置
        for info, _ in tracks[1:]:
            cmd += [*decoder_args(info), "-i", info["path"]]
        parts, cur = [bg], "bg"
        for i, (info, tl) in enumerate(tracks):
            nxt = "v" if i == len(tracks) - 1 else f"o{i}"
            parts.append(f"[{i + 1}:v]scale={tl['w']}:{tl['h']}:flags=lanczos,format=yuva420p[av{i}];"
                         f"[{cur}][av{i}]overlay={tl['x']}:{tl['y']}:eof_action=pass:format=auto"
                         + (",format=yuv420p" if nxt == "v" else "") + f"[{nxt}]")
            cur = nxt
        fc = ";".join(parts)
        if plan.get("audio_path"):  # 各動画に本人の声だけが入っている場合はミックス済み音声を使う
            cmd += ["-i", plan["audio_path"]]
            audio_map = f"{len(tracks) + 1}:a"
    elif m["has_video"]:
        if lay:  # 右下隅などに縮小配置
            fg = (f"[1:v]scale={lay['w']}:{lay['h']}:flags=lanczos,format=yuva420p[av];"
                  f"[bg][av]overlay={lay['x']}:{lay['y']}:eof_action=pass:format=auto,format=yuv420p[v]")
        else:
            fg = (f"[1:v]scale={W}:{H}:force_original_aspect_ratio=decrease,format=yuva420p[av];"
                  f"[bg][av]overlay=(W-w)/2:(H-h)/2:eof_action=pass:format=auto,format=yuv420p[v]")
        if not m["has_alpha"]:  # 透過なしの場合は右下にワイプ表示
            fg = (f"[1:v]scale=-2:{H // 2 // 2 * 2}[av];"
                  f"[bg][av]overlay=W-w-{W // 40}:H-h-{H // 20}:eof_action=pass,format=yuv420p[v]")
        fc = f"{bg};{fg}"
    else:
        fc = bg.replace("[bg]", "[v]")
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", audio_map, "-t", f"{total:.6f}",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-r", f"{fps.numerator}/{fps.denominator}",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dest)]
    log("  ffmpeg で合成中…")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg の合成に失敗しました")

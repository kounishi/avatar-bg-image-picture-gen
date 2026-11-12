"""タイムライン（CSV/JSON）と編集ソフト用 XML（FCPXML / Premiere 用 FCP7 XML）の出力。"""
from __future__ import annotations

import csv
import json
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

from .media import avatar_tracks
from .util import fmt_jp, fmt_tc


def write_timeline(plan: dict, out: Path):
    rows = []
    for s in plan["scenes"]:
        rows.append({
            "index": s["index"], "start": fmt_tc(s["start"]), "end": fmt_tc(s["end"]),
            "start_jp": fmt_jp(s["start"]), "duration_sec": round(s["end"] - s["start"], 3),
            "start_frame": s["start_frame"], "end_frame": s["end_frame"],
            "type": s["type"], "file": s["file"], "title": s["title"],
            "source_image": s.get("image") or "", "narration": s["narration"],
        })
    with open(out / "timeline.csv", "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    (out / "timeline.json").write_text(json.dumps({"fps": plan["fps"], "size": plan["size"],
                                                   "duration": plan["duration"], "scenes": rows},
                                                  ensure_ascii=False, indent=2), encoding="utf-8")


def _rt(frames: int, fd: Fraction) -> str:
    t = frames * fd
    return f"{t.numerator}/{t.denominator}s" if t.denominator != 1 else f"{t.numerator}s"


def write_fcpxml(plan: dict, out: Path, name: str):
    """DaVinci Resolve / Final Cut Pro 用 FCPXML 1.9"""
    W, H = plan["size"]
    fps = Fraction(plan["fps"])
    fd = 1 / fps
    m = plan["media"]
    total = plan["total_frames"]
    res = [f'<format id="r1" frameDuration="{fd.numerator}/{fd.denominator}s" width="{W}" height="{H}"/>',
           f'<format id="r2" name="FFVideoFormatRateUndefined" width="{W}" height="{H}"/>']
    if m["has_video"]:
        res.append(f'<format id="r3" frameDuration="{fd.numerator}/{fd.denominator}s" '
                   f'width="{m["width"]}" height="{m["height"]}"/>')
    av_uri = Path(m["path"]).as_uri()
    res.append(f'<asset id="av" name="{escape(Path(m["path"]).name)}" src="{escape(av_uri)}" start="0s" '
               f'duration="{_rt(total, fd)}" hasVideo="{1 if m["has_video"] else 0}" '
               f'{"format=" + chr(34) + "r3" + chr(34) + " " if m["has_video"] else ""}'
               f'hasAudio="1" audioSources="1" audioChannels="{m["audio_channels"]}" audioRate="{m["audio_rate"]}"/>')
    tracks = avatar_tracks(plan)
    shared_audio = not plan.get("audio_path")  # 2人とも同じ会話音声なら2本目以降は映像だけ使う
    for i, (info, _) in enumerate(tracks[1:], start=2):
        res.append(f'<format id="r{i + 2}" frameDuration="{fd.numerator}/{fd.denominator}s" '
                   f'width="{info["width"]}" height="{info["height"]}"/>')
        res.append(f'<asset id="av{i}" name="{escape(Path(info["path"]).name)}" '
                   f'src="{escape(Path(info["path"]).as_uri())}" start="0s" duration="{_rt(total, fd)}" hasVideo="1" '
                   f'format="r{i + 2}" hasAudio="{0 if shared_audio else 1}"'
                   + ("" if shared_audio else f' audioSources="1" audioChannels="{info["audio_channels"]}" '
                      f'audioRate="{info["audio_rate"]}"') + "/>")
    clips = []
    for s in plan["scenes"]:
        aid = f"img{s['index']}"
        res.append(f'<asset id="{aid}" name="{escape(Path(s["file"]).name)}" src="{escape(Path(s["file"]).as_uri())}" '
                   f'start="0s" duration="0s" hasVideo="1" format="r2"/>')
        inner = ""
        if s["index"] == 1:
            lane = 1 if m["has_video"] else -1
            inner = (f'<asset-clip ref="av" lane="{lane}" offset="0s" duration="{_rt(total, fd)}" start="0s" '
                     f'name="avatar">{_fcp_transform(plan)}</asset-clip>')
            for i, (info, tl) in enumerate(tracks[1:], start=2):
                inner += (f'<asset-clip ref="av{i}" lane="{i}" offset="0s" duration="{_rt(total, fd)}" start="0s" '
                          f'name="avatar{i}">{_fcp_transform_for(plan, info, tl)}</asset-clip>')
        clips.append(f'<video ref="{aid}" offset="{_rt(s["start_frame"], fd)}" '
                     f'duration="{_rt(s["end_frame"] - s["start_frame"], fd)}" start="0s" '
                     f'name="{escape(s["title"][:40])}">{inner}</video>')
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.9">\n'
           f'<resources>\n{chr(10).join(res)}\n</resources>\n'
           f'<library><event name="{escape(name)}"><project name="{escape(name)}">\n'
           f'<sequence format="r1" duration="{_rt(total, fd)}" tcStart="0s" tcFormat="NDF" '
           f'audioLayout="stereo" audioRate="48k"><spine>\n{chr(10).join(clips)}\n</spine></sequence>\n'
           '</project></event></library>\n</fcpxml>\n')
    (out / "timeline_resolve_fcp.fcpxml").write_text(xml, encoding="utf-8")


def _fcp_transform(plan: dict) -> str:
    """FCPXML の位置・拡大率。position は画面高さ=100 の単位で中心からのずれ（上が正）。
    scale は「画面に収まるよう自動縮小（fit）」された状態を 1 とする倍率。"""
    m, lay = avatar_tracks(plan)[0]
    return _fcp_transform_for(plan, m, lay)


def _fcp_transform_for(plan: dict, m: dict, lay) -> str:
    if not lay or not m["has_video"]:
        return ""
    W, H = plan["size"]
    fit = min(W / m["width"], H / m["height"])
    cx, cy = lay["x"] + lay["w"] / 2, lay["y"] + lay["h"] / 2
    px, py = (cx - W / 2) / H * 100, (H / 2 - cy) / H * 100
    s = lay["scale"] / fit
    return f'<adjust-transform position="{px:.4f} {py:.4f}" scale="{s:.5f} {s:.5f}"/>'


def _premiere_motion(plan: dict, lay=None) -> str:
    """Premiere（FCP7 XML）の Basic Motion。scale は元素材ピクセル基準の %、center は画面中心からの割合。"""
    lay = lay or avatar_tracks(plan)[0][1]
    if not lay:
        return ""
    W, H = plan["size"]
    cx, cy = lay["x"] + lay["w"] / 2, lay["y"] + lay["h"] / 2
    return ("<filter><effect><name>Basic Motion</name><effectid>basic</effectid><effectcategory>motion</effectcategory>"
            "<effecttype>motion</effecttype><mediatype>video</mediatype>"
            f"<parameter><parameterid>scale</parameterid><name>Scale</name><value>{lay['scale'] * 100:.3f}</value></parameter>"
            f"<parameter><parameterid>center</parameterid><name>Center</name><value><horiz>{(cx - W / 2) / W:.5f}</horiz>"
            f"<vert>{(cy - H / 2) / H:.5f}</vert></value></parameter></effect></filter>")


def _pathurl(p: str) -> str:
    return "file://localhost/" + quote(Path(p).resolve().as_posix(), safe="/:")


def write_premiere_xml(plan: dict, out: Path, name: str):
    """Adobe Premiere Pro 用 FCP7 XML（xmeml v4）"""
    W, H = plan["size"]
    fps = Fraction(plan["fps"])
    tb = round(float(fps))
    ntsc = "TRUE" if fps.denominator != 1 else "FALSE"
    rate = f"<rate><timebase>{tb}</timebase><ntsc>{ntsc}</ntsc></rate>"
    m = plan["media"]
    total = plan["total_frames"]
    v1 = []
    for s in plan["scenes"]:
        k, dur = s["index"], s["end_frame"] - s["start_frame"]
        v1.append(
            f'<clipitem id="clipitem-{k}"><name>{escape(Path(s["file"]).name)}</name><enabled>TRUE</enabled>'
            f'<duration>{dur}</duration>{rate}<start>{s["start_frame"]}</start><end>{s["end_frame"]}</end>'
            f'<in>0</in><out>{dur}</out>'
            f'<file id="file-{k}"><name>{escape(Path(s["file"]).name)}</name><pathurl>{_pathurl(s["file"])}</pathurl>'
            f'{rate}<duration>{dur}</duration><media><video><samplecharacteristics><width>{W}</width>'
            f'<height>{H}</height></samplecharacteristics></video></media></file></clipitem>')
    av_file = (f'<file id="file-av"><name>{escape(Path(m["path"]).name)}</name><pathurl>{_pathurl(m["path"])}</pathurl>'
               f'{rate}<duration>{total}</duration><media>'
               + (f'<video><samplecharacteristics><width>{m["width"]}</width><height>{m["height"]}</height>'
                  f'</samplecharacteristics></video>' if m["has_video"] else "")
               + f'<audio><channelcount>{m["audio_channels"]}</channelcount></audio></media></file>')
    common = f'<duration>{total}</duration>{rate}<start>0</start><end>{total}</end><in>0</in><out>{total}</out>'
    tracks_v = [f'<track>{"".join(v1)}</track>']
    if m["has_video"]:
        tracks_v.append(f'<track><clipitem id="clipitem-av-v"><name>avatar</name><enabled>TRUE</enabled>{common}'
                        f'{av_file}{_premiere_motion(plan)}</clipitem></track>')
        a_file = '<file id="file-av"/>'
    else:
        a_file = av_file
    tracks = avatar_tracks(plan)
    extra_a = []
    for i, (info, tl) in enumerate(tracks[1:], start=2):  # 2人目以降のアバター（V3〜）
        f_xml = (f'<file id="file-av{i}"><name>{escape(Path(info["path"]).name)}</name>'
                 f'<pathurl>{_pathurl(info["path"])}</pathurl>{rate}<duration>{total}</duration><media>'
                 f'<video><samplecharacteristics><width>{info["width"]}</width><height>{info["height"]}</height>'
                 f'</samplecharacteristics></video><audio><channelcount>{info["audio_channels"]}</channelcount>'
                 f'</audio></media></file>')
        tracks_v.append(f'<track><clipitem id="clipitem-av{i}-v"><name>avatar{i}</name><enabled>TRUE</enabled>'
                        f'{common}{f_xml}{_premiere_motion(plan, tl)}</clipitem></track>')
        if plan.get("audio_path"):  # 各動画に本人の声だけが入っている場合は音声も並べる
            extra_a.append(f'<track><clipitem id="clipitem-av{i}-a1"><name>avatar{i} audio</name>'
                           f'<enabled>TRUE</enabled>{common}<file id="file-av{i}"/><sourcetrack><mediatype>audio'
                           f'</mediatype><trackindex>1</trackindex></sourcetrack></clipitem></track>')
    tracks_a = []
    for ch in range(1, min(2, m["audio_channels"]) + 1):
        tracks_a.append(f'<track><clipitem id="clipitem-av-a{ch}"><name>avatar audio</name><enabled>TRUE</enabled>'
                        f'{common}{a_file}<sourcetrack><mediatype>audio</mediatype><trackindex>{ch}</trackindex>'
                        f'</sourcetrack></clipitem></track>')
        a_file = '<file id="file-av"/>'
    tracks_a += extra_a
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">\n'
           f'<sequence id="sequence-1"><name>{escape(name)}</name><duration>{total}</duration>{rate}'
           f'<media><video><format><samplecharacteristics>{rate}<width>{W}</width><height>{H}</height>'
           f'<pixelaspectratio>square</pixelaspectratio></samplecharacteristics></format>\n'
           + "\n".join(tracks_v) + '\n</video><audio>\n' + "\n".join(tracks_a)
           + '\n</audio></media></sequence>\n</xmeml>\n')
    (out / "timeline_premiere.xml").write_text(xml, encoding="utf-8")

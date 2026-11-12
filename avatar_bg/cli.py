"""コマンドライン: plan（枚数とタイミングの提示）→ render（画像生成・差し込み・出力）"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .util import fmt_jp, fmt_tc, parse_size

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANNEL = "JAHICニュースCh"
TYPE_JP = {"title": "タイトル", "slide": "解説スライド", "article_image": "記事内画像"}


def log(*a, **k):
    print(*a, **k, flush=True)


def _setup_env():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    bundle = os.environ.get("AVATAR_BG_CA_BUNDLE") or str(ROOT / ".venv" / "ca-bundle.pem")
    if Path(bundle).exists():
        for k in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
            os.environ.setdefault(k, bundle)


# ---------------------------------------------------------------- plan
def cmd_plan(a) -> Path:
    from .align import align
    from .article import fetch_article
    from .media import audio_fingerprint, avatar_layout, mix_audio, probe
    from .planner import build_plan
    from .script import parse_script, speakers_of

    medias = [Path(m).resolve() for m in a.media]
    media, script = medias[0], Path(a.script).resolve()
    for p in (*medias, script):
        if not p.exists():
            raise SystemExit(f"ファイルが見つかりません: {p}")
    if len(medias) > 2:
        raise SystemExit("アバター動画は 2 つまで指定できます。")
    duo = len(medias) == 2
    size = parse_size(a.size)
    name = script.stem if duo else media.stem
    out = Path(a.out or (ROOT / "output" / name)).resolve()
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    (out / "custom").mkdir(exist_ok=True)

    log(f"[1/5] メディア解析: {media.name}")
    info = probe(media)
    if not info["has_audio"]:
        raise SystemExit("音声トラックが見つかりません。")
    log(f"  長さ {fmt_jp(info['duration'])} / "
        + (f"映像 {info['width']}x{info['height']} {info['fps']}fps / 透過 {'あり' if info['has_alpha'] else 'なし'}"
           if info["has_video"] else "音声のみ"))
    if a.minutes and abs(info["duration"] / 60 - a.minutes) > max(1.0, a.minutes * 0.3):
        log(f"  ※ 指定時間 {a.minutes} 分と実際の長さに差があります（実際の長さを優先します）")

    log(f"[2/5] シナリオ解析: {script.name}")
    sentences = parse_script(script)
    log(f"  {len(sentences)} 文 / 見出し {len({s['section'] for s in sentences if s['section_title']})} 個")
    speakers = speakers_of(sentences)
    avatars, audio_src = None, media
    if duo:
        # 2人掛け合い：話者IDとファイル名を対応づけ、登場順に 左下・右下 に配置
        order = _assign_avatars(medias, speakers)
        infos = {m: (info if m == media else probe(m)) for m in medias}
        avatars = [{"path": str(m), "speaker": sp, "side": side, "info": infos[m]}
                   for (m, sp), side in zip(order, ("bottom-left", "bottom-right"))]
        for av in avatars:
            log(f"  アバター {'左下' if av['side'] == 'bottom-left' else '右下'}: {Path(av['path']).name}"
                + (f"（話者 {av['speaker']}）" if av["speaker"] else ""))
        fps_ = [audio_fingerprint(m, min(30.0, info["duration"] / 3)) for m in medias]
        if fps_[0] and len(set(fps_)) == 1:
            log("  2つの動画には同じ会話音声が入っています（1本分の音声を使います）")
        else:
            audio_src = work / "mixed_audio.wav"
            log("  各動画の音声を1本にミックスします")
            mix_audio(medias, audio_src)
    elif speakers:
        log(f"  ※ 台本に話者が {len(speakers)} 人います。2人掛け合いにする場合は --media に動画を2つ指定してください")

    article = None
    if a.url:
        log(f"[3/5] 記事取得: {a.url}")
        try:
            article = fetch_article(a.url, work / "article_images", log=log)
            log(f"  タイトル: {article['title']}")
            log(f"  見出し {len(article['headings'])} 個 / 使用可能な記事内画像 {len(article['images'])} 枚")
        except Exception as e:  # noqa: BLE001
            log(f"  記事の取得に失敗しました（{e}）。シナリオのみで作成します")
    else:
        log("[3/5] 記事URLの指定なし（シナリオのみで作成）")

    log("[4/5] ナレーションのタイミング解析")
    method = align(sentences, audio_src, info["duration"], work, a.whisper_model, log)

    log("[5/5] シーン構成")
    plan = build_plan(sentences, article, info, size, a)
    plan["align_method"] = method
    plan["name"] = name
    plan["variants"] = []
    if duo:
        plan["avatars"] = avatars
        if audio_src != media:
            plan["audio_path"] = str(audio_src)
        plan["avatar_layout"] = plan["avatar_bbox"] = None
        lays = _duo_layouts(avatars, size, a.duo_avatar_height, a.avatar_margin)
        plan["avatar_bboxes"] = [(l or {}).get("bbox") for l in lays]
        for av, lay in zip(avatars, lays):
            av["layout"] = lay
    else:
        plan["avatar_layout"] = avatar_layout(info, *size, pos=a.avatar_pos, height=a.avatar_height,
                                              margin=a.avatar_margin)
        plan["avatar_bbox"] = (plan["avatar_layout"] or {}).get("bbox")
    if a.shorts and size[0] > size[1]:
        # ショート動画版：同じシーン・タイミングで縦長にレイアウトし直す
        ssize = (1080, 1920)
        if duo:
            lays = _duo_layouts(avatars, ssize, a.shorts_duo_avatar_height, a.avatar_margin)
            plan["variants"].append({"name": "shorts", "size": list(ssize), "avatar_layout": None,
                                     "avatar_bbox": None, "avatar_layouts": lays,
                                     "avatar_bboxes": [(l or {}).get("bbox") for l in lays]})
        else:
            lay = avatar_layout(info, *ssize, pos=a.shorts_avatar_pos, height=a.shorts_avatar_height,
                                margin=a.avatar_margin)
            plan["variants"].append({"name": "shorts", "size": list(ssize), "avatar_layout": lay,
                                     "avatar_bbox": (lay or {}).get("bbox")})
    plan["thumbnail"] = {"channel": a.channel, "title": a.thumb_title or "", "avatar_time": a.thumb_avatar_time,
                         "background": None}
    plan["sentences"] = sentences
    (out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_prompts(plan, out)
    show_plan(plan, out)
    return out


def _assign_avatars(medias: list, speakers: list) -> list:
    """話者IDを含むファイル名の動画をその話者に割り当てる。返り値は登場順の [(動画, 話者)]。"""
    from .util import norm_chars

    left, order = list(medias), []
    for sp in speakers:
        key = norm_chars(sp)
        hit = next((m for m in left if key and key in norm_chars(m.stem)), None)
        if hit is None and left and len(order) < len(medias):
            hit = next((m for m in left if not any(norm_chars(s) in norm_chars(m.stem) for s in speakers)), None)
        if hit is not None:
            left.remove(hit)
            order.append((hit, sp))
    order += [(m, None) for m in left]  # 台本に話者がない／対応しない動画は指定順
    return order[:2]


def _duo_layouts(avatars: list, size, height: float, margin: float) -> list:
    from .media import avatar_layout

    return [avatar_layout(av["info"], *size, pos=av["side"], height=height, margin=margin) for av in avatars]


def show_plan(plan: dict, out: Path):
    sc = plan["scenes"]
    c = plan["counts"]
    avg = plan["duration"] / len(sc)
    log("")
    log("=" * 72)
    log(f" 作成する背景画像: {len(sc)} 枚  （平均 {avg:.1f} 秒/枚）")
    log("   内訳: " + " / ".join(f"{TYPE_JP.get(k, k)} {v}枚" for k, v in c.items()))
    log(f" 動画サイズ {plan['size'][0]}x{plan['size'][1]} / {plan['fps']}fps / 長さ {fmt_jp(plan['duration'])}"
        f" / タイミング推定: {'音声認識' if plan['align_method'] == 'whisper' else '無音検出＋文字数比'}")
    for av in plan.get("avatars") or []:
        lay = av.get("layout") or {}
        b = lay.get("bbox") or [0, 0, 0, 0]
        log(f" アバター {'左下' if av['side'] == 'bottom-left' else '右下'} {Path(av['path']).stem} / "
            f"縮小率 {lay.get('scale', 1) * 100:.0f}% / 表示範囲 x={b[0]}〜{b[2]}, y={b[1]}〜{b[3]}")
    if plan.get("avatars"):
        x0 = max(b[2] for b in plan["avatar_bboxes"] if b and (b[0] + b[2]) / 2 < plan["size"][0] / 2)
        x1 = min(b[0] for b in plan["avatar_bboxes"] if b and (b[0] + b[2]) / 2 >= plan["size"][0] / 2)
        log(f"   解説は2人の間の中央（x={x0}〜{x1}）に表示します")
    if plan.get("avatar_bbox"):
        b = plan["avatar_bbox"]
        lay = plan.get("avatar_layout") or {}
        log(f" アバター配置 {lay.get('pos', '')} / 縮小率 {lay.get('scale', 1) * 100:.0f}% / "
            f"表示範囲 x={b[0]}〜{b[2]}, y={b[1]}〜{b[3]}（文字はこの領域を避けて配置）")
    for v in plan.get("variants", []):
        b = v.get("avatar_bbox") or next((x for x in v.get("avatar_bboxes") or [] if x), None)
        log(f" ＋ショート動画版 {v['size'][0]}x{v['size'][1]}（同じ {len(sc)} 枚構成・同じタイミング、出力先 shorts\\）"
            + (f" アバター y={b[1]}〜{b[3]}" if b else ""))
        if plan["duration"] > 180:
            log("   ※ 3分を超えるため YouTube ショートとしては投稿できません（通常の縦動画になります）")
    th = plan.get("thumbnail") or {}
    log(f" ＋サムネイル 1280x720" + (" / ショート用 1080x1920" if plan.get("variants") else "")
        + f"（チャンネル名「{th.get('channel', '')}」＋記事タイトル＋アバター）")
    log("=" * 72)
    for s in sc:
        log(f" #{s['index']:02d} {fmt_tc(s['start'])} - {fmt_tc(s['end'])} ({s['end'] - s['start']:5.1f}s) "
            f"[{TYPE_JP.get(s['type'], s['type'])}] {s['title'][:36]}")
    log("=" * 72)
    log(f" 構成ファイル: {out / 'plan.json'}")
    log("  （タイトル・箇条書き・type を編集してから render できます。")
    log(f"   ChatGPT で作った画像を使う場合は {out / 'custom'}\\scene_03.png のように置いてください。")
    log(f"   参考プロンプト: {out / 'chatgpt_prompts.md'}）")


def _write_prompts(plan: dict, out: Path):
    W, H = plan["size"]
    b = plan.get("avatar_bbox")
    side = ""
    bbs = [x for x in plan.get("avatar_bboxes") or [] if x]
    if len(bbs) > 1:
        side = (f"画面の左下と右下（それぞれ横幅の約{round(max(x[2] - x[0] for x in bbs) / W * 100)}%・"
                f"高さの下側約{round((H - min(x[1] for x in bbs)) / H * 100)}%）は2人のアバターを重ねるため、"
                "主要な要素は中央に置いてください。")
    elif b:
        side = "画面右側" if (b[0] + b[2]) / 2 > W / 2 else "画面左側"
        side = f"{side}の約{round((b[2] - b[0]) / W * 100)}%はアバターを重ねるため、主要な要素を置かず余白にしてください。"
    lines = ["# ChatGPT 画像生成用プロンプト（任意）", "",
             "解説スライドの代わりにイラストを使いたいシーンだけ、下のプロンプトを ChatGPT に貼って画像を生成し、",
             f"`{out / 'custom'}` に `scene_番号.png`（例: scene_03.png）で保存してから render してください。", ""]
    for s in plan["scenes"]:
        pts = "、".join(s["bullets"]) if s["bullets"] else s["narration"][:120]
        lines += [f"## scene_{s['index']:02d}  ({fmt_tc(s['start'])} - {fmt_tc(s['end'])})  {s['title']}", "",
                  "```",
                  f"YouTube解説動画の背景画像を作ってください。横{W}×縦{H}px（比率 {W}:{H}）。",
                  f"テーマ: {s['title']}",
                  f"内容: {pts}",
                  "シンプルでフラットな図解イラスト。文字は入れないか、入れる場合は短い日本語の見出しのみ。",
                  side, "```", ""]
    (out / "chatgpt_prompts.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- render
def cmd_render(a, out: Path = None):
    out = Path(out or a.out).resolve()
    plan_path = out / "plan.json"
    if not plan_path.exists():
        raise SystemExit(f"{plan_path} がありません。先に plan を実行してください。")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    _render_one(plan, out, out / "custom", a, "")
    for v in plan.get("variants", []):
        vplan = json.loads(json.dumps(plan))
        vplan.update({k: v[k] for k in ("size", "avatar_layout", "avatar_bbox")})
        if vplan.get("avatars"):  # 2人掛け合い：縦画面用の配置に差し替え
            for av, lay in zip(vplan["avatars"], v.get("avatar_layouts") or []):
                av["layout"] = lay
            vplan["avatar_bboxes"] = v.get("avatar_bboxes")
        vout = out / v["name"]
        vout.mkdir(exist_ok=True)
        (vout / "work").mkdir(exist_ok=True)
        log(f"\n--- {v['name']} 版（{v['size'][0]}x{v['size'][1]}） ---")
        # ChatGPT 画像は縦横比が違うので custom\shorts\ に置いたものだけ使う
        _render_one(vplan, vout, out / "custom" / v["name"], a, f"_{v['name']}")
    if not getattr(a, "no_thumbnail", False):
        cmd_thumb(a, out, plan)
    log("完了しました。")


def cmd_thumb(a, out: Path = None, plan: dict = None):
    from .thumbnail import make_thumbnails

    out = Path(out or a.out).resolve()
    plan_path = out / "plan.json"
    if plan is None:
        if not plan_path.exists():
            raise SystemExit(f"{plan_path} がありません。先に plan を実行してください。")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    th = plan.setdefault("thumbnail", {"channel": DEFAULT_CHANNEL, "title": "", "avatar_time": None,
                                       "background": None})
    changed = regen = False
    for key, attr in (("channel", "channel"), ("title", "thumb_title"), ("avatar_time", "thumb_avatar_time"),
                      ("pick", "pick")):
        v = getattr(a, attr, None) if a.cmd == "thumb" else None
        if v is not None:
            th[key] = v
            changed = True
            regen |= key != "pick"
    if changed:  # thumb コマンドで指定した値は plan.json にも保存
        _save_thumb_settings(plan_path, th)
    from .thumbnail import select_thumbnail

    if a.cmd == "thumb" and not regen and getattr(a, "pick", None) and (out / "thumbnail_1.jpg").exists():
        select_thumbnail(out, plan, a.pick, log=log)  # 選び直すだけなら作り直さない
        return
    log("サムネイルを作成中（表情 3 パターン）")
    make_thumbnails(plan, out, log=log)
    for d in [out] + [out / v["name"] for v in plan.get("variants", [])]:
        if (d / "thumbnail_choices.jpg").exists():
            log(f"  3パターンの比較: {d / 'thumbnail_choices.jpg'}")
    pick = int(th.get("pick") or 1)
    if (a.cmd == "run" and not a.yes and (out / "thumbnail_2.jpg").exists() and sys.stdin.isatty()):
        ans = input("サムネイルの表情を選んでください 1:標準の笑顔 / 2:口を大きく開けた笑顔 / 3:口を閉じた笑顔"
                    f" [Enter={pick}]: ").strip()
        if ans in ("1", "2", "3") and int(ans) != pick:
            pick = th["pick"] = int(ans)
            _save_thumb_settings(plan_path, th)
    select_thumbnail(out, plan, pick, log=log)
    log(f"  （あとから選び直す場合: thumb -o \"{out}\" --pick 2）")


def _save_thumb_settings(plan_path: Path, th: dict):
    saved = json.loads(plan_path.read_text(encoding="utf-8"))
    saved["thumbnail"] = th
    plan_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")


def _render_one(plan: dict, out: Path, custom_dir: Path, a, suffix: str):
    from .compose import compose
    from .export import write_fcpxml, write_premiere_xml, write_timeline
    from .slides import Renderer

    img_dir = out / "images"
    img_dir.mkdir(exist_ok=True)
    for old in img_dir.glob("scene_*.png"):
        old.unlink()

    r = Renderer(plan)
    n = len(plan["scenes"])
    log(f"背景画像を作成中（{n} 枚）")
    for s in plan["scenes"]:
        custom = next((p for ext in ("png", "jpg", "jpeg", "webp")
                       for p in (custom_dir / f"scene_{s['index']:02d}.{ext}",
                                 custom_dir / f"scene_{s['index']:03d}.{ext}") if p.exists()), None)
        im = r.render(s, custom)
        dest = img_dir / f"scene_{s['index']:03d}.png"
        im.save(dest, optimize=False)
        s["file"] = str(dest)
        if custom:
            s["type"] = "custom"
        log(f"  {s['index']:3d}/{n} {dest.name} [{TYPE_JP.get(s['type'], s['type'])}]", end="\r")
    log("")

    name = (plan.get("name") or Path(plan["media"]["path"]).stem) + suffix
    write_timeline(plan, out)
    write_fcpxml(plan, out, name)
    write_premiere_xml(plan, out, name)
    log(f"タイムライン: {out / 'timeline.csv'}")
    log(f"DaVinci Resolve / Final Cut 用: {out / 'timeline_resolve_fcp.fcpxml'}")
    log(f"Premiere Pro 用: {out / 'timeline_premiere.xml'}")
    if not getattr(a, "no_mp4", False):
        dest = out / f"{name}_with_bg.mp4"
        compose(plan, out, dest, crf=getattr(a, "crf", 18), log=log)
        log(f"合成済み動画: {dest}")

def build_parser():
    p = argparse.ArgumentParser(prog="avatar_bg", description="アバター動画用の背景画像を自動作成して差し込むツール")
    sub = p.add_subparsers(dest="cmd", required=True)

    def plan_args(sp):
        sp.add_argument("--media", "-m", required=True, nargs="+",
                        help="音声＋アバター合成ファイル（mov / mp4 / webm / m4a など）。"
                             "2人掛け合いの場合は2つ指定（台本の話者IDを含むファイル名なら自動で対応づけ）")
        sp.add_argument("--script", "-s", required=True, help="シナリオテキストファイル")
        sp.add_argument("--url", "-u", help="元記事のURL")
        sp.add_argument("--size", default="1920x1080", help="動画サイズ（既定 1920x1080。4k / shorts / 1280x720 なども可）")
        sp.add_argument("--minutes", type=float, help="想定の動画時間（分）。実際の長さとの差のチェックに使用")
        sp.add_argument("--out", "-o", help="出力フォルダ（既定 output/<メディア名>）")
        sp.add_argument("--target-sec", type=float, default=20, help="背景1枚あたりの目安秒数（既定 20）")
        sp.add_argument("--min-sec", type=float, default=8, help="1枚の最短秒数（既定 8）")
        sp.add_argument("--max-sec", type=float, default=45, help="1枚の最長秒数（既定 45）")
        sp.add_argument("--max-bullets", type=int, default=4, help="スライドの箇条書き最大数（既定 4）")
        sp.add_argument("--max-article-images", type=int, default=99, help="記事内画像の最大使用枚数")
        sp.add_argument("--image-threshold", type=float, default=0.2, help="記事内画像を採用する類似度の下限")
        sp.add_argument("--whisper-model", default="small",
                        help="音声認識モデル（tiny/base/small/medium/large-v3、none で無効）")
        sp.add_argument("--theme", choices=["dark", "light"], default="dark")
        sp.add_argument("--no-title-card", dest="title_card", action="store_false", help="冒頭のタイトル画像を作らない")
        sp.add_argument("--fps", default="30", help="音声のみの場合のフレームレート")
        sp.add_argument("--avatar-pos", choices=["bottom-right", "bottom-left", "center", "original"],
                        default="bottom-right", help="透過アバターの配置（既定 右下隅。original は元動画のまま中央）")
        sp.add_argument("--avatar-height", type=float, default=0.8,
                        help="アバターの見えている部分の高さ＝画面高さ×この値（既定 0.8）")
        sp.add_argument("--avatar-margin", type=float, default=0.02,
                        help="画面端からの余白（画面幅に対する割合、既定 0.02）")
        sp.add_argument("--no-shorts", dest="shorts", action="store_false",
                        help="ショート動画（縦 1080x1920）版を出力しない")
        sp.add_argument("--shorts-avatar-pos", choices=["center", "bottom-right", "bottom-left", "original"],
                        default="center", help="ショート版のアバター配置（既定 下部中央）")
        sp.add_argument("--shorts-avatar-height", type=float, default=0.42,
                        help="ショート版のアバターの高さ＝画面高さ×この値（既定 0.42）")
        sp.add_argument("--duo-avatar-height", type=float, default=0.5,
                        help="2人掛け合いのアバターの高さ＝画面高さ×この値（既定 0.5。1人の場合は --avatar-height）")
        sp.add_argument("--shorts-duo-avatar-height", type=float, default=0.3,
                        help="2人掛け合いのショート版のアバターの高さ（既定 0.3）")
        sp.add_argument("--channel", default=DEFAULT_CHANNEL, help=f"サムネイルのチャンネル名（既定 {DEFAULT_CHANNEL}）")
        thumb_args(sp)

    def thumb_args(sp):
        sp.add_argument("--thumb-title", help="サムネイルのタイトル（既定は記事タイトル）")
        sp.add_argument("--thumb-avatar-time", type=float,
                        help="サムネイルに使うアバターのフレーム位置（秒。既定は動画全体から最も典型的な表情のコマを自動選択）")

    sp = sub.add_parser("plan", help="背景画像の枚数とタイミングを算出して提示（画像はまだ作らない）")
    plan_args(sp)
    sr = sub.add_parser("render", help="plan.json に従って画像作成・差し込み・出力")
    sr.add_argument("--out", "-o", required=True)
    sr.add_argument("--no-mp4", action="store_true", help="合成済み mp4 を作らない")
    sr.add_argument("--crf", type=int, default=18)
    sr.add_argument("--no-thumbnail", action="store_true", help="サムネイルを作らない")
    st = sub.add_parser("thumb", help="サムネイルだけを作り直す")
    st.add_argument("--out", "-o", required=True)
    st.add_argument("--channel", help="チャンネル名")
    st.add_argument("--pick", type=int, choices=[1, 2, 3],
                    help="使う表情 1:標準の笑顔 2:口を大きく開けた笑顔 3:口を閉じた笑顔（選び直すだけなら作り直しません）")
    thumb_args(st)
    sa = sub.add_parser("run", help="plan → 枚数を確認 → render を続けて実行")
    plan_args(sa)
    sa.add_argument("--yes", "-y", action="store_true", help="確認せずに続行")
    sa.add_argument("--no-mp4", action="store_true")
    sa.add_argument("--crf", type=int, default=18)
    sa.add_argument("--no-thumbnail", action="store_true", help="サムネイルを作らない")
    return p


def main(argv=None):
    _setup_env()
    a = build_parser().parse_args(argv)
    if a.cmd == "plan":
        cmd_plan(a)
    elif a.cmd == "render":
        cmd_render(a)
    elif a.cmd == "thumb":
        cmd_thumb(a)
    else:
        out = cmd_plan(a)
        if not a.yes:
            ans = input("\nこの枚数・タイミングで背景画像を作成して差し込みますか？ [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                log(f"中止しました。plan.json を編集後、render --out \"{out}\" で続行できます。")
                return
        cmd_render(a, out)


if __name__ == "__main__":
    main()

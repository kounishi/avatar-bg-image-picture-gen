# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 概要

`avatar_bg` は、ブログ記事 URL・シナリオテキスト・アバター動画（透過 mov 等）から、YouTube 動画用の背景画像を自動作成し、タイムライン（CSV/JSON/FCPXML/Premiere XML）・合成済み mp4・サムネイルを出力する Python CLI。ユーザー向けの詳細な仕様・オプションは README.md（日本語）にある。UI 文言・ログ・コメントはすべて日本語で書かれているので、それに合わせる。

## 制約: OpenAI API は使わない

- OpenAI API（従量課金の API キー、`openai` パッケージ、`api.openai.com` への直接リクエスト）は使用しない。`requirements.txt` にも追加しない。
- 画像生成・文章生成などで OpenAI のモデルが必要な場合は、ChatGPT のサブスクリプションの範囲内で動く **Codex CLI**（`codex`）を経由して使う。
- 現状の「ChatGPT で作った画像を使う」機能（`chatgpt_prompts.md` を出力 → 生成画像を `custom/scene_NN.png` に置く）もこの方針に沿ったもので、API 呼び出しに置き換えないこと。

## 環境・コマンド

- Windows / PowerShell 前提。Python 3.9（`.venv`）、ffmpeg/ffprobe が PATH に必要。
- セットアップ: `powershell -ExecutionPolicy Bypass -File .\setup.ps1`
  - 社内ネットワークの SSL 対策として Windows 信頼済みルート証明書入りの `.venv\ca-bundle.pem` を作る。`cli._setup_env()` がこれ（または `AVATAR_BG_CA_BUNDLE`）を `REQUESTS_CA_BUNDLE` 等に設定する。証明書検証を無効化する方向の修正はしないこと。
- 実行（サブコマンド: `plan` / `render` / `thumb` / `run`）:
  ```powershell
  .\.venv\Scripts\python -m avatar_bg plan -m avatar.mov -s scenario.txt -u https://... -o output\vol01
  .\.venv\Scripts\python -m avatar_bg render -o output\vol01
  .\.venv\Scripts\python -m avatar_bg thumb -o output\vol01 --pick 2
  .\.venv\Scripts\python -m avatar_bg run -m a.mov b.mov -s script.txt -u https://...   # 2人掛け合い
  ```
- 動作確認用の入力素材は `sozai-000N/`（動画・ナレーション txt・`対象記事URL.txt`。git 管理外）、出力は既定で `output\<名前>\`。
- テスト・リンタ・ビルド設定は存在しない。確認は実素材で `plan`/`render` を走らせて行う。高速に回すなら `--whisper-model base`（または `none`）、`--no-mp4`、`--no-shorts`、`--no-thumbnail` を使う。

## アーキテクチャ

処理は **plan → (plan.json を手編集可) → render** の 2 段階で、`plan.json` が両者をつなぐ唯一の中間表現。`render` と `thumb` は `plan.json` だけを入力に動くので、plan 時に決めた値（レイアウト、サムネイル設定など）は必ず `plan.json` に保存する設計になっている。

- `cli.py` — 全体のオーケストレーション。各モジュールは関数内で遅延 import（faster-whisper 等の重い依存を必要時のみ読む）。
  - `cmd_plan`: probe → `script.parse_script` → (2人なら話者とファイル名の対応づけ・音声の同一判定/ミックス) → `article.fetch_article` → `align.align` → `planner.build_plan` → アバター配置計算 → `plan.json` と `chatgpt_prompts.md` を出力。
  - `cmd_render`: メイン版を `_render_one` で描画後、`plan["variants"]`（現状はショート版 1080x1920）ごとに plan をディープコピーしてサイズ・アバター配置を差し替え、`<out>/shorts/` に同じ処理を再実行。最後に `cmd_thumb`。
  - `_render_one`: `slides.Renderer` で各シーン画像を描画（`custom/scene_NN.png` があれば差し替え、ショート版は `custom/shorts/`）→ `export` でタイムライン/XML → `compose` で ffmpeg 合成。
- `align.py` — faster-whisper の文字単位書き起こし（`work/transcript_<model>.json` にキャッシュ）とシナリオ文をバイグラム類似度で照合し各文の開始/終了秒を決める。照合率 30% 未満や失敗時は無音検出による比例配分（`align_proportional`）にフォールバック。
- `planner.py` — 文をシーンに分割（見出し・段落・目標秒数、2人モードでは聞き手の発言開始を区切り候補に）、各シーンに type（`title` / `slide` / `article_image`）と記事内画像を類似度で割り当て（1 画像 1 回まで）。シーン境界は前後の文の中間でフレーム単位に隙間なく接続。
- `media.py` — ffmpeg/ffprobe ラッパー。アルファチャンネルからキャラクター輪郭の bbox を検出し、`avatar_layout`（x, y, w, h, scale, bbox）を計算。このレイアウトが slides（文字をアバターと重ならない側に配置）、compose、export XML のすべてで共有される。
- `slides.py`（Pillow による背景描画）、`thumbnail.py`（瞳・口検出による表情 3 パターンの選択とサムネイル描画）、`export.py`（CSV/JSON/FCPXML/FCP7 XML）、`compose.py`（ffmpeg 合成）、`util.py`（類似度、タイムコード、サイズ解析）。

### 1人 / 2人モードの分岐

`--media` が 2 つなら 2人掛け合いモード。plan.json では 1人モードが `avatar_layout`/`avatar_bbox` を使い、2人モードは `avatars[]`（各要素に `layout`）と `avatar_bboxes` を使い `avatar_layout` は `None`。レイアウト関連を変更するときは両モードと、ショート版 variant（`avatar_layouts` / `avatar_bboxes`）の差し替え処理をすべて追うこと。

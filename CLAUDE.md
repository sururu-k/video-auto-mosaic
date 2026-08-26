# automosaic

実写動画に自動でモザイクをかける道具。日本の刑法175条対応が目的。
入口は localhost の Web アプリで、素材の投入から完成品の受け取りまでを1か所で行う。

## 最初に読むもの

**[RULES.md](RULES.md) を必ず読むこと。** 全員に適用される規律が書いてある。
とくに次の2点は、これを守らない作業は受け付けない。

- **モザイクが漏れる方向の壊れ方が致命的で、塗り過ぎ側は許容できる。**
  判断がつかないときは塞ぐ・止める。黙って素通しを作らない。
- **証拠が証言に優先する。** 「対応した」「テストが通った」は証言にすぎない。
  同一ターンで取得した生の実行ログだけを根拠にする。未確認は未確認と書く。

## 構成

```
automosaic/
  cli.py         2パス（検出 -> 描画）の入口。python -m automosaic
  detector.py    ONNX 推論。NudeNet 系の重み
  temporal.py    トラッキング / 結合 / デスパイク / 補間 / 橋渡し / 膨張
  render.py      モザイクの描画。ブロック化と平面の詰め替え
  video.py       ffmpeg のパイプ。probe / デコード / エンコード
  corrections.py 手修正（add / remove）の適用
  review.py      検査 UI のサーバ。/timeline もここが配信する
  segmenter.py   セグメンテーション推論。書けているが cli から呼ばれていない
  webapp/        Web アプリ。処理は python -m automosaic をサブプロセスで叩く
frontend/        TypeScript + Preact。esbuild で束ねてビルド結果をコミットする
tools/           補助スクリプト。verify_output.py は漏れ検証用
docs/            設計と実測の記録。番号順に読むと経緯が分かる
```

## よく使うコマンド

```
# Web アプリを立てる（既定 127.0.0.1:8770、トークンは毎回ランダム）
.venv\Scripts\python.exe -m automosaic.webapp

# CLI で焼く
.venv\Scripts\python.exe -m automosaic 入力.mp4 -o 出力.mp4

# テスト（pytest は使っていない。素の Python スクリプト）
.venv\Scripts\python.exe tests\test_render.py
.venv\Scripts\python.exe tests\test_temporal_fixes.py
.venv\Scripts\python.exe tests\test_video_cli_fixes.py
.venv\Scripts\python.exe tests\test_review.py
.venv\Scripts\python.exe tests\test_webapp.py
node tests\test_frontend.mjs

# フロントエンド（canonical repository の最新 master を取り込んでから作る）
git fetch origin
git rebase origin/master
npm --prefix frontend ci
node frontend/build.mjs
npm --prefix frontend run check-build
npm --prefix frontend run typecheck
```

`origin` は canonical repository を指す remote 名。fork を `origin` にしている場合は、
canonical repository を指す remote（通常は `upstream`）に読み替える。
フロントを変更する PR は、**その remote の最新 master を取り込んでから**ビルド結果をコミットする。
共有モジュールを変えると複数のバンドルが変わるため、`node build.mjs` が更新した成果物を
選り分けずすべて確認する。`node tests\test_frontend.mjs` は最初に同じ同期検査を行い、
`frontend/node_modules` が無ければ成功扱いにせず `npm ci` を案内して停止する。
同期検査は remote 名ではなく URL が `sururu-k/video-auto-mosaic` を指すことを確認し、
その remote の master が HEAD の祖先であることも確認する。
canonical repository をミラー経由で取得する場合は
`AUTOMOSAIC_FRONTEND_BASE_REF=<remote>/master` を明示する。

`PYTHONIOENCODING=utf-8` を設定しないと日本語の出力が化ける。

## 今わかっている状態

- `docs/07-audit-2026-08-23.md` —— 再監査。壊れ方の一覧
- `docs/08-backlog.md` —— 次に直すもの。**回転メタデータで全フレームが壊れる件が最優先**
- `docs/09-mosaic-quality.md` —— モザイク生成の質の批評。
  **フレーム単位 recall は 35%。出荷判定は存在しない**

道具としての現状は「人手で全編チェックする作業を速くするもの」であって、
無人で焼けるものではない。そう書くこと。数字を盛らない。

## 環境

- Windows。PowerShell と Git Bash の両方が使える
- GPU は AMD の DirectML。**CUDA 前提のものは動かない**
- **`git push` は固まることがある。** 固まったら `gh` の API 経由（Git Data API）で回避する
- `data/myvideo5` `data/bench3` は既存の作業データ。読むだけ

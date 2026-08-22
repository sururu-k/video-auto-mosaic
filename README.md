# video-auto-mosaic

実写動画の局部を自動検出し、モザイクを付与するツール。刑法175条コンプライアンス目的。

動画を投げると、モザイクをかけて出す。対象は**実写動画のみ**（静止画・二次元は対象外）。

## 使い方

```bash
python -m automosaic 入力.mp4
# -> 入力_mosaic.mp4 が出る
```

よく使う形:

```bash
# 出力先を指定し、レポートも出す
python -m automosaic in.mp4 -o out.mp4 --report report.json

# 検出結果を保存しておくと、モザイクの見た目を変えるときに検出をやり直さずに済む
python -m automosaic in.mp4 --detections det.json
python -m automosaic in.mp4 --detections det.json --reuse-detections --block 32

# 先に検出だけ回して、どれくらい当たっているか見る
python -m automosaic in.mp4 --detect-only --report report.json

# 速度が要るとき。3フレームおきに検出して間は補間で埋める（取りこぼしの危険は上がる）
python -m automosaic in.mp4 --frame-step 3

# まず短く試す
python -m automosaic in.mp4 --limit-frames 300
```

主なオプション:

| オプション | 既定 | 意味 |
|---|---|---|
| `--conf` | 0.12 | 信頼度しきい値。Recall 優先で既定より下げてある |
| `--classes` | default | `default`(露出のみ) / `conservative`(COVERED も含む) / カンマ区切り |
| `--block` | 自動 | モザイクのブロックサイズ px。自動は長辺÷100 |
| `--mode` | pixelize | `pixelize`（ブロック平均色）/ `black`（塗り潰し） |
| `--margin-scale` | 1.0 | 膨張マージンの倍率。潰し足りないときに上げる |
| `--memory` | 6 | トラック端を前後に保持するフレーム数 |
| `--max-gap` | 12 | トラック継続を許す欠損フレーム数 |
| `--bridge-max` | 150 | 前後が覆われた未処理区間を埋める最大フレーム数 |
| `--frame-step` | 1 | Nフレームおきに検出 |
| `--crf` | 16 | x264 CRF。16〜18 が視覚的に無劣化 |
| `--provider` | auto | `auto` / `cpu` / `dml` / `cuda` |
| `--device-id` | 1 | DirectML のアダプタ番号 |

## 仕組み

2パス構成。中間フレームをディスクに展開しないので、長尺でもディスクを食わない。

```
パス1  長辺を推論解像度に縮めてデコード -> 全フレーム検出 -> 座標だけJSONに保持
       ↓
       幾何フィルタ -> トラッキング -> デスパイク -> 補間 -> frame memory -> 橋渡し -> 膨張
       ↓
パス2  原寸YUVを読み直す -> Y/U/V平面上で直接モザイク -> ffmpegへ書き戻し
```

- **検出漏れは補間で埋める。** バッチ処理なので未来フレームも使える。前後どちらにも検出があるフレームは必ず埋まるため、単発〜数フレームの漏れは構造的にゼロになる
- **トラックが分断されても素通しにしない。** 前後が覆われている未処理区間は、両側の外接矩形で塞ぐ。埋めなかった区間は必ずレポートに出す
- **モザイクの格子はフレーム座標に固定。** bbox 基準にすると対象が動くたびに格子がずれてチラつく
- **色劣化を避けるため RGB に変換しない。** planar YUV のまま扱う。4:2:0 の彩度平面は座標とブロックを //2 する
- **音声・字幕・チャプタは元ファイルから stream copy。** 元ファイルを2番目の入力として渡して持ってくるので無劣化。色空間タグは ffprobe で読んだ値を明示的に付け直す

## 環境

現状の検証環境は AMD Ryzen 5 8600G + Radeon 760M / RX 560、NVIDIA なし。CUDA は使えないので ONNX Runtime の DirectML を使う。

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
winget install Gyan.FFmpeg
gh release download v3.4-weights --repo notAI-tech/NudeNet --pattern 640m.onnx --dir weights
```

重みの直リンクは GitHub の認証ページに弾かれるので `gh release download` を使う。

利用可能なプロバイダの確認:

```bash
python -m automosaic --list-providers
```

### 実測スループット（640x640 推論、デコード時間は含まない）

| モデル | プロバイダ | fps | 60分@30fps |
|---|---|---|---|
| 640m | CPU | 4.3 | 6.9 時間 |
| 640m | DirectML (device 1) | 14.2 | 2.1 時間 |
| 320n | CPU | 109 | 0.27 時間 |
| 320n | DirectML | 88 | 0.34 時間 |

320n は小さすぎて DirectML のオーバーヘッドが勝つ。ただし入力320px では 1080p/4K に小さく写る対象を落とすので、**本番は 640m を使う**。夜間バッチ前提。

## テスト

```bash
.venv\Scripts\python.exe tests\test_render.py     # 描画と時間方向の検証（ffmpeg不要）
.venv\Scripts\python.exe tests\bench_detector.py  # スループット計測
.venv\Scripts\python.exe tests\bench_devices.py   # DirectML アダプタ別の計測
```

検出器を通さずに描画パスだけ確認したいときは、手書きの検出結果を使う:

```bash
python tests\make_fake_detections.py det.json --gap-from 40 --gap-to 55
python -m automosaic sample.mp4 --detections det.json --reuse-detections
```

## 前提（確定事項）

- **ツールは公開・配布しない。** 自己使用のみ。成果物の動画だけが外に出る
  ライセンスはkにしない

## 設計上の絶対条件

1. **1フレームの検出漏れ＝法的に致命的。** 過剰に潰すのは許容、漏らすのは不可。Recall優先・Precision妥協
2. **ガウシアンブラーを使わない。** 復元可能性は法的リスクの中核（FLMASK事件）。ブロック平均色ピクセライズか塗り潰し
3. **「判断できない ＝ 潰す」。** 判断できないから何もしない、は絶対にやらない
4. **出力側の独立検証を出荷ゲートにする。** 学習に使っていない別モデルで再スキャンし、何も検出されないことを出荷条件にする（未実装）
5. **学習データにWebスクレイピングを一切使わない。** 年齢確認済みの素材のみ。台帳化必須

## 現状と残件

動くのは「動画を投げるとモザイクがかかって出る」ところまで。

未実装:
- 出力側の独立再スキャンによる出荷ゲート（設計上いちばん重要）
- 人手レビュー UI（信頼度ヒートマップ、フリップブック表示、ブラシ修正）
- 二重系（DensePose 等で人体から局部位置を推定する第2系統）
- セグメンテーションによるマスク化（現状は bbox なので矩形に潰れる）

実素材での精度は未検証。`--detect-only --report` で当たり具合を見てから `--conf` と `--margin-scale` を調整すること。

## ドキュメント

| ファイル | 内容 |
|---|---|
| [docs/00-market-and-oss-survey.md](docs/00-market-and-oss-survey.md) | OSS・商用API・国内業界実務・法的前提の調査（2026-08-22） |
| [docs/01-technical-design.md](docs/01-technical-design.md) | 技術設計メモ |

## 調査の結論

- 実写動画に局部モザイクを付与する完成品OSSは存在しない
- 商用モデレーションAPIは6社すべて使えない（**局部の座標を返すベンダーが0社**）
- 市販編集ソフトに完全自動の機能はない（すべて人がマスクを打って追尾させる半自動）
- 局部検出の既製重みはほぼ全てUltralytics YOLO由来＝上流AGPL-3.0。**ただし本件は非配布なので影響なし**
- 公開データセットは事実上ゼロ。自前構築が必要になるが、既製重みが使えるため後倒しにできる

**設計目標を「全自動で焼き込む」に置かない。** 現実解は「全フレームで候補領域を提示し、人間の全編チェックを高速化するツール」。

## 法的な注意

本リポジトリの文書は公開情報の整理であり、法的助言ではない。
実験的なレポジトリである為法的責任は分離して考えること


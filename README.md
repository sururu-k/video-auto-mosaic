# video-auto-mosaic

実写動画の局部を自動検出し、モザイクを付与するツール。刑法175条コンプライアンス目的。

動画を投げると、モザイクをかけて出す。対象は**実写動画のみ**（静止画・二次元は対象外）。

## 前提（確定事項）

- **ツールは公開・配布しない。** 自己使用のみ。成果物の動画だけが外に出る
  ライセンスは気にしない

## 使い方

```bash
python -m automosaic 入力.mp4
# -> 入力_mosaic.mp4 が出る
```

検出は重いので、まず検出だけ回して結果を保存し、見た目の調整は再利用するのが基本の流れ。

```bash
# 1. 検出（重い。60分尺で数時間）
python -m automosaic in.mp4 --detect-only --detections det.json --report report.json

# 2. 見た目を詰める（検出をやり直さないので数十秒）
python -m automosaic in.mp4 --detections det.json --reuse-detections --block 12
python -m automosaic in.mp4 --detections det.json --reuse-detections --margin-scale 0.2

# 3. 漏れを手で直す（下記「手修正」）
python -m automosaic in.mp4 --detections det.json --reuse-detections --corrections corrections.json
```

まず短く試すなら `--limit-frames 300`。

### 主なオプション

| オプション | 既定 | 意味 |
|---|---|---|
| `--infer-size` | 960 | 推論の画素予算（長辺ではない）。素材の縦横比を保った入力サイズをここから決める。**ここが検出率に一番効く** |
| `--conf` | 0.06 | 信頼度しきい値。実写ではスコアが低く出るので下げてある |
| `--tta` | off | 水平反転した推論もマージ。推論2倍、検出+16% |
| `--classes` | default | `default`(露出のみ) / `conservative`(COVERED も含む) / カンマ区切り |
| `--block` | 自動 | モザイクのブロックサイズ px。自動は長辺÷100 |
| `--mode` | pixelize | `pixelize`（ブロック平均色）/ `black`（塗り潰し） |
| `--margin-scale` | 0.35 | 膨張マージンの倍率。大きいと潰しすぎ、小さいと輪郭が出る |
| `--margin-cap` | 16 | 膨張マージンの絶対上限 px |
| `--motion-weight` | 2.0（`--estimate-gaps` 無しでは 1.0 に絞られる） | 動く対象への追従。この分は `--margin-cap` の外 |
| `--estimate-gaps` | off | 検出が途切れた区間を推定で埋める（後述） |
| `--despike` | off | 単発かつスコア0.35未満のトラックを丸ごと捨てる。既定オフ（実測: 確実に映っている区間の実観測125件を誤って捨て、うち40件はそのフレームが素通しになっていた。`docs/09-mosaic-quality.md` S4）。有効にすると捨てた場所を必ず表示・レポートに出す |
| `--allow-short-detections` | off | 実尺より短い検出結果でも描画を続ける（後述） |
| `--corrections` | — | 手修正 JSON を反映する |
| `--crf` | 16 | x264 CRF。16〜18 が視覚的に無劣化 |
| `--provider` | auto | `auto` / `cpu` / `dml` / `cuda` |
| `--device-id` | 1 | DirectML のアダプタ番号 |

`--frame-step N` で N フレームおきの検出にすれば速くなるが、数フレームしか映らない対象を取りこぼす。

### 既定は「検出できた箇所だけ」

検出が途切れた区間を推定で埋める機構（memory / 橋渡し / 不確かさ膨張）は持っているが、**既定では無効**。位置が当てずっぽうの領域が増えて塗り過ぎになるため。有効にするのは `--estimate-gaps`。

実素材での差:

| | 推定のみで覆う区間 |
|---|---|
| 既定（検出できた箇所だけ） | 14件 / 237フレーム |
| `--estimate-gaps` | 20件 / 491フレーム |

「1フレームでも漏らさない」を優先するなら `--estimate-gaps`、塗り過ぎを避けるなら既定。**この選択は法的な posture の選択なので、意識して決めること。**

`--estimate-gaps` を付けない実行では、推定で広げる側のオプションが次のように絞られる。
絞ったものは起動時に「`--estimate-gaps` 無しのため推定で広げる設定を絞りました」と表示される。

| オプション | 引数の既定 | `--estimate-gaps` 無しでの実効値 |
|---|---|---|
| `--memory` | 6 | 2 |
| `--memory-before` | 0（= `--memory` と同じ） | 2 |
| `--bridge-max` | 150 | 0 |
| `--hold-growth` | 0.5 | 0.0 |
| `--motion-weight` | 2.0 | 1.0 |

**コマンドラインで明示指定した値はこの絞り込みより優先される。**
`--memory 20` と書けば `--estimate-gaps` 無しでも 20 のまま走り、「明示指定を優先」と表示される。

### 検出結果が実尺より短いとき

`--limit-frames` で作った検出結果や、途中保存（`--checkpoint-every` が書く `complete: false`）を
`--reuse-detections` で本編に流用すると、検出のない後半が素通しになる。
そのため既定では**エラーで止める**。

- 途中保存（`complete: false`）は再利用できない。`--resume` で検出を最後まで走らせる
- 解像度の違う検出 JSON も弾く（座標が別の位置に載るため）
- 未検出区間があると承知のうえで焼くなら `--allow-short-detections`。
  この場合は不足の開始フレームと長さが警告に出る（直前フレームの領域を延ばすだけで、塞げている保証はない）

## 手修正

自動検出には限界がある。漏れた箇所は座標を手で与えて塞ぐ。全フレームに打つ必要はなく、数フレームおきに打てば、あいだは補間で埋まる。

```bash
# 1. 「推定のみで覆っている区間」からフレームを抜く（検出器が効いていない区間）
python tools/extract_review_frames.py in.mp4 --report report.json \
  --detections det.json --out-dir review_frames --stride 12

# 2. 抜いたフレームを見て座標を JSON に書く
#    [{"frame": 577, "box": [495, 240, 80, 90], "class": "MALE_GENITALIA_EXPOSED"}]

# 3. 補間して corrections.json に展開
python tools/annotations_to_corrections.py annotations.json -o corrections.json --width 640 --height 480

# 4. 反映して描画
python -m automosaic in.mp4 --detections det.json --reuse-detections --corrections corrections.json
```

`box` に `null` を書くと「ここには無い」の意味になり、そこで補間が止まる。対象が画面から消えた後もモザイクが伸びるのを防ぐ。

### レビュー UI

ブラウザで見ながら直したい場合。ローカルサーバが立つ（標準ライブラリのみ、127.0.0.1 固定）。

```bash
python -m automosaic.review 入力.mp4 --rendered 出力.mp4 \
  --detections det.json --corrections corrections.json
# -> http://127.0.0.1:8765/
```

タイムラインが緑（検出できて塗れている）・黄（推定のみ）・赤（未処理）で塗られ、黄と赤の区間リストからジャンプできる。`M` で漏れモードに入り動画上をクリックすると矩形を置き、`1` でこのフレームだけ、`2` で以降 N フレームに適用。`,` `.` でコマ送り、`[` `]` でサイズ調整、`D` で削除、`G` で次の推定のみ区間へ。保存は自動。

停止中は `<video>` でなくサーバから取り直した厳密なフレーム画像に差し替わる。`<video>` のシークはフレーム厳密でないため、注釈の座標が1フレームずれるのを防いでいる。プレビューのモザイクは出力と同じ `render.apply_regions()` を通しているので、画面で隠れているものは出力でも隠れる。

手修正が溜まったら学習データとして書き出せる。

```bash
python -m automosaic.review in.mp4 --corrections corrections.json --export-dataset dataset/
```

`images/` + `labels/`（YOLO正規化）+ `classes.txt` + `dataset.yaml`。同フレームの自動検出の矩形も一緒に入れる（手修正だけだと「他には何も写っていない」という誤った教師になるため）。推定由来の矩形は実観測ではないので除外。

## 仕組み

2パス構成。中間フレームをディスクに展開しないので、長尺でもディスクを食わない。

```
パス1  推論解像度に合わせてデコード -> 全フレーム検出 -> 座標だけJSONに保持
       ↓
       幾何フィルタ -> トラッキング -> デスパイク -> トラックレット結合
       -> 補間 -> (memory / 橋渡し) -> 膨張 -> 手修正の反映
       ↓
パス2  原寸YUVを読み直す -> Y/U/V平面上で直接モザイク -> ffmpegへ書き戻し
```

設計上の要点:

- **検出漏れは補間で埋める。** バッチ処理なので未来フレームも使える。前後どちらにも検出があるフレームは必ず埋まる
- **途切れ途切れのトラックは結合する。** 位置と大きさが近いトラックを3秒以内なら1本に繋ぐ。「モザイク有り・なし・有り」の直接の対策。離れた別対象までは繋がないよう中心間距離と大きさの比で歯止めをかけている
- **memory 区間は矩形を固定しない。** 端点の速度で外挿し、実観測から離れた分だけ矩形を広げる。固定すると動く対象に置いていかれる（実際に速度34px/フレームの区間で20フレーム固定して漏らしていた）
- **マージンは静的分と動き分に分ける。** 静的分は上限で抑え、動き分は上限の外に出す。速く動く場面だけ厚くなり、静止時の大きさは変わらない
- **信頼度の項は素の (1 - score) を使わない。** このモデルはスコアが全体的に低く、そのまま使うと基礎マージンが何倍にも膨らむ
- **重複検出は外接矩形に統合する（`--merge union`）。** NMS は重なった候補を「消す」ので、消された側がはみ出していた分の被覆が失われる
- **NMS はクラスをまたがない。** 局部と臀部のように重なる部位が互いを消し合うと片方を取りこぼす
- **モザイクの格子はフレーム座標に固定。** bbox 基準にすると対象が動くたびに格子がずれてチラつく
- **色劣化を避けるため RGB に変換しない。** planar YUV のまま扱う。4:2:0 の彩度平面は座標とブロックを //2 する
- **音声・字幕・チャプタは元ファイルから stream copy。** 元ファイルを2番目の入力として渡すので無劣化。色空間タグは ffprobe で読んだ値を明示的に付け直す

## レポート

`--report` を付けると統計と、見るべき区間が JSON で出る。

| 項目 | 意味 |
|---|---|
| `uncovered_ranges` | 何も塗られていない区間 |
| `estimated_only_ranges` | **実観測が1つも無く推定だけで塗っている区間。位置が当てずっぽうに近く、人手レビューの最優先対象** |
| `review_frames` | 低信頼・面積急変など、個別に怪しいフレーム |

## 環境

AMD Ryzen 5 8600G + Radeon 760M / RX 560、NVIDIA なし。CUDA が使えないので ONNX Runtime の DirectML を使う。

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
winget install Gyan.FFmpeg
gh release download v3.4-weights --repo notAI-tech/NudeNet --pattern 640m.onnx --dir weights
```

重みの直リンクは GitHub の認証ページに弾かれるので `gh release download` を使う。
利用可能なプロバイダは `python -m automosaic --list-providers` で確認できる。

### 実測スループット（デコード時間は含まない）

| 設定 | fps | 60分@30fps |
|---|---|---|
| 640 / CPU | 4.3 | 6.9 時間 |
| 640 / DirectML | 14.2 | 2.1 時間 |
| 960 / DirectML | — | 約 4.5 時間 |
| 1280 / DirectML | — | 約 7.5 時間 |
| 1280 + TTA / DirectML | — | 約 15 時間 |

DirectML のアダプタは `device_id=1` が最速だった（`tests/bench_devices.py` で実測）。夜間バッチ前提。

### 推論解像度の効果（実素材 640x480 / 1768フレーム）

| 設定 | 検出フレーム | 未処理フレーム |
|---|---|---|
| 640 | 845 | 316 |
| **960** | **1179 (+39.5%)** | 72 |
| 1280 | 1188 (+40.6%) | 66 |
| 640 + TTA | 980 (+16.0%) | 72 |
| 1280 + TTA | 1232 (+45.8%) | 13 |

ONNX の入力が可変（`['batch', 3, 'height', 'width']`）なので推論解像度を自由に変えられる。640x480 の素材はモデルの学習解像度と同じため、そのままだと実質等倍で入力されており拡大の恩恵をまったく受けていなかった。**960 が費用対効果のピーク。**

## テストと計測

```bash
.venv\Scripts\python.exe tests\test_render.py            # 描画と時間方向（ffmpeg不要、24件）
.venv\Scripts\python.exe tests\test_temporal_fixes.py    # 時間方向の監査回帰（ffmpeg不要、13件）
.venv\Scripts\python.exe tests\test_video_cli_fixes.py   # CLI/デコードの監査回帰（ffmpeg要、15件）
.venv\Scripts\python.exe tests\test_review.py            # レビューUI（52件）
.venv\Scripts\python.exe tests\bench_detector.py         # スループット
.venv\Scripts\python.exe tests\bench_devices.py          # DirectML アダプタ別
```

`tools/` は開発用ユーティリティ。

| ツール | 用途 |
|---|---|
| `compare_configs.py` | 検出設定を振って Recall を比較 |
| `analyze_detections.py` | 検出結果の分布としきい値感度、未処理区間の中身 |
| `margin_preview.py` | マージン設定ごとの面積倍率を焼く前に数値で確認 |
| `extract_review_frames.py` | 推定のみ区間からフレームを抜く |
| `annotations_to_corrections.py` | 目視で出した座標を補間して corrections に展開 |
| `extract_eval_frames.py` / `annotate_eval.py` / `eval_recall.py` | 正解ラベルを作って Recall を測る |
| `make_fake_detections.py` | 検出器を通さず描画パスだけ確認する |

## 設計上の絶対条件

1. **ガウシアンブラーを使わない。** 復元可能性は法的リスクの中核（FLMASK事件）。ブロック平均色ピクセライズか塗り潰し
2. **判断できない区間は黙って素通ししない。** 埋めないなら必ずレポートに出す
3. **出力側の独立検証を出荷ゲートにする。** 学習に使っていない別モデルで再スキャンし、何も検出されないことを出荷条件にする（未実装）
4. **学習データに Web スクレイピングを一切使わない。** 年齢確認済みの素材のみ。台帳化必須

## 現状と残件

動くのは「動画を投げるとモザイクがかかって出る」ところまで。実素材1本での調整を1周した。

未実装:
- セグメンテーションによるマスク化（現状は bbox なので矩形に潰れる）
- 出力側の独立再スキャンによる出荷ゲート
- 二重系（DensePose 等で人体から局部位置を推定する第2系統）
- 手修正データでのファインチューン

## ドキュメント

| ファイル | 内容 |
|---|---|
| [docs/00-market-and-oss-survey.md](docs/00-market-and-oss-survey.md) | OSS・商用API・国内業界実務・法的前提の調査 |
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

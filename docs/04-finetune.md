# 手修正データでのファインチューン手順

このリポジトリでは**実行しない**。手順だけ残す。理由は環境にある。

- この機械は AMD（Ryzen 5 8600G + Radeon 760M / RX 560）で NVIDIA が無い
- Windows では ROCm が使えないため、PyTorch は CPU 実行になる
- YOLO11 の学習を CPU で回すのは現実的でない

学習は GPU のある環境（クラウドのレンタルGPU、あるいは NVIDIA のある機械）で行う。
このリポジトリの役割は**学習データを作るところまで**。

## なぜファインチューンが要るか

既製の NudeNet 640m は実写でスコアが低く出る。実素材でのスコア中央値は 0.15 前後で、
`--conf 0.06` まで下げないと半分近く取りこぼす。下げれば誤検出が増える。
これは「この素材の分布を学習していない」ことの表れで、しきい値をいじっても解決しない。

競合（動画自動モザイクくん）は MMDetection で**自前学習**しており、Deepmosaic は
**10万枚超**で学習している。既製モデルの組み合わせでは埋まらない差がここにある。

一方、うちは**修正するたびに正解データが増える**。漏れを見つけて座標を与える作業が、
そのまま「この場面のこの位置に局部がある」という教師になる。案件をこなすほど
データが溜まる構造になっている。

## データの作り方

```bash
python -m automosaic.review 素材.mp4 \
  --detections det.json --corrections corrections.json \
  --export-dataset dataset/
```

出力:

```
dataset/
  images/000440.png      元動画のフレーム（原寸）
  labels/000440.txt      YOLO形式（クラスID 中心x 中心y 幅 高さ、すべて0〜1）
  classes.txt
  dataset.yaml           ultralytics 形式
```

同じフレームに自動検出の矩形があればそれも含める。手修正だけだと
「他には何も写っていない」という誤った教師になるため。ただし補間・memory・橋渡し
由来の矩形は**実観測ではない**ので除外している。

### 座標の確認

正規化の掛け違いや xy の取り違えは数字を眺めても気づけない。実際に描いて見る。

```bash
python tools/check_dataset.py dataset/ --count 8
# dataset/_check/ に矩形を描いた画像が出る
```

マイビデオ-5 の 384 フレームで確認済み。矩形13件すべて 0〜1 の範囲内、4px未満なし、
描画位置も対象と一致していた。

## 学習（GPU のある環境で）

```bash
pip install ultralytics
yolo detect train \
  model=yolo11m.pt \
  data=dataset/dataset.yaml \
  epochs=100 imgsz=960 batch=8 \
  device=0
```

`imgsz=960` にするのは、実測で 640→960 が検出フレーム数を約4割増やしたため。
推論時と学習時の解像度を揃える。

### NudeNet の重みから始めるか、素の YOLO11 から始めるか

NudeNet の重みは `.pt` も配布されている（`gh release download v3.4-weights --repo
notAI-tech/NudeNet --pattern 640m.pt`）。ただし YOLOv8 系で、クラスが18個ある。

- **18クラスを維持して転移学習**: 既存の検出力を保ったまま、この素材の分布に寄せる。
  データが少ないうちはこちらが安全
- **3クラスに絞って学習**: 対象クラスだけに集中できるが、少量データだと過学習しやすい

数百フレーム規模なら前者。数千枚溜まってから後者を試す。

## 学習後の組み込み

学習結果を ONNX にして、既存のパイプラインに差し込む。

```bash
yolo export model=runs/detect/train/weights/best.pt format=onnx dynamic=True opset=17
```

`dynamic=True` は必須。推論解像度を変えられなくなると、実測で最も効いた
「解像度を上げる」手が使えなくなる。

```bash
python -m automosaic 素材.mp4 --model weights/finetuned.onnx --infer-size 960
```

クラス名は `automosaic/detector.py` の `LABELS` と順序を合わせること。
18クラスを維持したなら変更不要。

## 効果の測り方

**しきい値やスコアで測ってはいけない。** モデルが変われば分布が変わるので比較できない。

測るべきは**出力を見て漏れているか**。同じ素材を新旧のモデルで処理し、
それぞれ目視検査にかけて漏れ件数を比べる。

```bash
# 出力からフレームを抜いて目視検査へ
python tools/extract_review_frames.py 素材.mp4 --report report.json \
  --detections det.json --out-dir review_frames --stride 5
```

検査の粒度は**5フレーム刻み以下**にする。実測で15フレーム刻みは半分見落とした。

## 注意

学習データの扱いは `docs/01-technical-design.md` の「児童ポルノ規制法 — 絶対条件」に従う。
特に **Webスクレイピングを一切行わない**、**年齢確認済みの素材のみ**、
**データセットは外に出さない**の3点。

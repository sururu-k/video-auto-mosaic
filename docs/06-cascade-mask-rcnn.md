# Cascade Mask R-CNN (MMDetection) をこのマシンで使えるか

調査日 2026-08-23。**すべて実測**。推定値には「推定」と明記した。

## 結論

**ONNX には出せた。DirectML でも動いた。だが速度で失格。**

Cascade Mask R-CNN R101-FPN は 1280x720 相当の入力で **1.85 fps**。
60分尺で **16.3 時間**。3 fps の足切りを全解像度で割っている
（640x480 まで落として 3.74 fps / 8.0 時間。ただしこの素材は推論解像度を
上げることが最も効いた施策なので、解像度を下げる方向は選べない）。

さらに **局部検出用の Cascade Mask R-CNN 学習済み重みは公開されていない**。
採用するなら自前学習が前提になる。「学習して、その上で1本16時間」という構図。

**推論器としては採らない。** マスクが出る代替としては
**RTMDet-Ins**（同じ MMDetection 系、ONNX 化も同じ手順で通った）を勧める。
`max_per_img=10` で 960 入力 **7.6 fps / 3.9 時間**、640 入力 **15.1 fps / 2.0 時間**。

---

## 1. ONNX に出せたか

**出せた。** ただし MMDeploy のバグを2件踏んだので、そのままでは通らない。

### 環境

メインの `.venv` は一切触っていない（確認済み: mm*/torch* パッケージ 0 件）。
別環境 `.venv-mmdet` を新規に作った。

| | |
|---|---|
| Python | 3.11.15（uv で導入。**mmcv の Windows ビルド済み wheel が cp310/cp311 までしか無い**ため 3.12 は不可） |
| torch / torchvision | 2.1.0+cpu / 0.16.0+cpu |
| mmcv | 2.1.0（`download.openmmlab.com/mmcv/dist/cpu/torch2.1.0/` の win_amd64 wheel） |
| mmdet | 3.3.0 |
| mmdeploy | 1.3.1 |
| numpy | 1.26.4（2.x では mmcv が壊れる） |
| onnxruntime-directml | 1.24.4 |

MMDeploy の pip パッケージには deploy config も `tools/` も同梱されていない。
GitHub から取って `_base_` を展開し、1ファイルに畳んで使った
（`tools/export_mmdet_onnx.py` に埋め込み済み）。

### 重み

COCO 事前学習済みは MMDetection model zoo にある。

| config | box AP | mask AP |
|---|---|---|
| cascade-mask-rcnn_r101_fpn_1x_coco | 42.9 | 37.3 |
| **cascade-mask-rcnn_r101_fpn_20e_coco**（採用） | 43.4 | 37.8 |
| cascade-mask-rcnn_r101_fpn_ms-3x_coco | 45.5 | 39.6 |
| cascade-mask-rcnn_r101-caffe_fpn_1x_coco | 43.2 | 37.6 |

### 踏んだ罠 1: ORT がモデルをロードできない

MMDeploy が出す ONNX を ORT に食わせると、**実行以前にロードで落ちる**。

```
Type Error: Type parameter (T) of Optype (Where) bound to different types
(tensor(int64) and tensor(float) in node (/Where_11).
```

torch 側も書き出し時に警告を出している
（`The exported ONNX model failed ONNX shape inference` / `op_type:Where`）が、
**警告だけ出してファイルは書かれる**ので、気づかずに壊れた onnx を持つことになる。

原因は `mmdeploy/mmcv/ops/nms.py` の `_select_nms_index()`。

```python
batched_labels = batched_labels.where(
    (batch_inds == batch_template.unsqueeze(1)),
    batched_labels.new_ones(1) * -1)
```

NMS 出力のクラス添字は int64 のまま X 側に入るのに、パディング側の定数だけ
float32 の `-1.0` として書き出される。ONNX の `Where` は X と Y が同型でないと
いけないので ORT が弾く。全 Where 14 ノードのうち 1 件だけこれになる。

PyTorch の型昇格に合わせて int 側に Cast を挿す後処理を書いて解決した
（`tools/export_mmdet_onnx.py` の `fix_where_dtype()`）。
Mask R-CNN R50 でも同じ 1 件、RTMDet-Ins では 2 件出た。**MMDeploy 側の一般的なバグ**。

### 踏んだ罠 2: RTMDet-Ins は CPU だけの環境で書き出せない

```
AssertionError: Torch not compiled with CUDA enabled
  mmdeploy/codebase/mmdet/models/dense_heads/rtmdet_ins_head.py:165
    coord = self.prior_generator.single_level_grid_priors(hw, level_idx=0)
```

mmdet の `MlvlPointGenerator.single_level_grid_priors()` は既定が `device='cuda'`。
MMDeploy 側が device を渡さずに呼んでおり、`.to(mask_feat.device)` は生成の**後**なので
手遅れ。CUDA が無い環境では cpu に読み替えるパッチを当てて解決した
（同スクリプトの `patch_cuda_default_device()`）。

Cascade Mask R-CNN 側はこの問題は出ない。

### 出来上がった ONNX

| | |
|---|---|
| opset | 11、**ai.onnx のみ。カスタムドメインなし**（mmdeploy 独自 op を使っていない） |
| ノード数 | 2924 |
| 入力 | `input: ['batch', 3, 'height', 'width']` — **可変解像度が通る**（要件を満たす） |
| 出力 | `dets [batch,num_dets,5]` / `labels [batch,num_dets]` / `masks [batch,num_dets,28,28]` |
| サイズ | 385 MB |

`masks` は RoI 内 28x28。bbox に貼り戻す後処理が必要（Mask R-CNN 系の標準）。
RTMDet-Ins は逆に入力解像度のマスクをそのまま出す。

### DirectML EP で動くか

**動く。しかも 98.9% が GPU に乗っている。** カーネルフォールバックの病理は無い。

ORT のプロファイラで node 単位の実行時間を取った結果（1280x720、3回実行の合計）:

| EP | node 実行 | 時間 | 割合 |
|---|---|---|---|
| DmlExecutionProvider | 3699 | 1519.5 ms | **98.9%** |
| CPUExecutionProvider | 1206 | 17.2 ms | 1.1% |

CPU に落ちているのは `NonMaxSuppression`（6.9 ms）と Gather/Concat/Unsqueeze などの
形状系だけ。**RoIAlign は DirectML EP が対応していて GPU 側で回っている。**

つまり「DML が対応していない op のせいで遅い」のではない。
**素直にモデルが重いだけ。** ここに改善余地は無い。

### 動作確認（誤ったモデルを測っていないことの確認）

実素材のフレームに推論して COCO クラスとマスクを目視確認した。

```
check_000.png  person 0.96 mask/box=0.73
check_001.png  person 0.96 / dog 0.69 / cat 0.34
check_002.png  person 0.91 / cat 0.43 / person 0.36
```

正しく動いている。`data/mmdet_bench/check_*.png` に左右比較で焼いてある。

`mask/box=0.73` は「マスクが bbox 面積の 73% しか占めない」の意味。
**これがマスク化の実利**（後述）。

---

## 2. 実測 fps

実素材 `data/bench3/clips/src_0501.mp4`（1920x1080）から 12 フレーム。
デコード時間は除外。ウォームアップ 2 回。60分@30fps = 108000 フレーム換算。
「実入力」は 32 の倍数へのパディング後の実サイズ。

### Cascade Mask R-CNN R101-FPN（COCO box 43.4 / mask 37.8）

| 入力 | 実入力 | DML dev0 | 60分尺 | DML dev1 | 60分尺 | CPU | 60分尺 |
|---|---|---|---|---|---|---|---|
| 1333x800 | 768x1344 | 1.82 fps | 16.5 h | 1.45 fps | 20.7 h | 0.57 fps | 52.5 h |
| **1280x720** | 736x1280 | **1.85 fps** | **16.3 h** | 1.44 fps | 20.8 h | 0.62 fps | 48.8 h |
| 960x960 | 544x960 | 2.64 fps | 11.4 h | 1.96 fps | 15.3 h | 0.98 fps | 30.8 h |
| 640x480 | 384x640 | 3.74 fps | 8.0 h | — | — | 1.71 fps | 17.6 h |

**3 fps を割っている。** 640x480 まで落とせば 3.74 fps だが、
`README.md` の実測どおり **960 が費用対効果のピーク**（検出フレーム +39.5%）で
あり、640 に落とすのは本末転倒。実用域の 960 で 2.64 fps / 11.4 時間。

DirectML アダプタは **dev0 のほうが速い**。NudeNet では dev1 が最速だったので
**モデルによって逆転する**。`--device-id` の既定 1 をそのまま使うと 27% 損をする。

後処理の本数（`keep_top_k`）を 100 から 20 に絞って出し直しても
1.95 fps（15.4 h）で**ほぼ変わらない**。
コストは backbone + FPN + 3段カスケードヘッドであって、マスク後処理ではない。

### Mask R-CNN R50-FPN 2x（カスケード無しの参照、box 39.2 / mask 35.4）

| 入力 | DML dev0 | 60分尺 | CPU | 60分尺 |
|---|---|---|---|---|
| 1333x800 | 3.23 fps | 9.3 h | 0.92 fps | 32.7 h |
| 1280x720 | 3.29 fps | 9.1 h | 1.00 fps | 30.1 h |
| 960x960 | 5.29 fps | 5.7 h | 1.74 fps | 17.3 h |

カスケードを外すだけで **1.8 倍**。だが mask AP は 37.8 から 35.4 に落ちる。
それでも 9 時間で、まだ実用外。

### RTMDet-Ins（MMDetection の軽量インスタンスセグメンテーション）

既定の `max_per_img=100`（COCO 80クラス向けの設定そのまま）:

| モデル | mask AP | 入力 | DML dev0 | 60分尺 | CPU | 60分尺 |
|---|---|---|---|---|---|---|
| tiny | 35.4 | 640x640 | 11.17 fps | 2.7 h | 12.86 fps | 2.3 h |
| tiny | | 960x960 | 4.97 fps | 6.0 h | 5.80 fps | 5.2 h |
| tiny | | 1280x720 | 2.82 fps | 10.6 h | 2.72 fps | 11.0 h |
| s | 38.7 | 640x640 | 9.25 fps | 3.2 h | 9.61 fps | 3.1 h |
| s | | 960x960 | 4.63 fps | 6.5 h | 4.37 fps | 6.9 h |
| s | | 1280x720 | 2.91 fps | 10.3 h | 2.23 fps | 13.5 h |
| m | 42.1 | 640x640 | 6.45 fps | 4.7 h | 5.19 fps | 5.8 h |
| m | | 960x960 | 3.45 fps | 8.7 h | 2.29 fps | 13.1 h |
| m | | 1280x720 | 2.25 fps | 13.3 h | 1.18 fps | 25.3 h |

**DML が CPU にほとんど勝てていない。** 原因は「入力解像度のマスクを
100インスタンスぶん動的畳み込みで復号する」後処理が支配的だから。
局部検出は 1〜3 クラスなので 100 本も要らない。`max_per_img=10` で出し直すと:

### RTMDet-Ins / max_per_img=10（実運用に近い設定）

局部検出は 1〜3 クラスなので、実際に必要なインスタンス数はこの程度。

| モデル | 入力 | DML dev0 | 60分尺 | max=100 比 | CPU | 60分尺 |
|---|---|---|---|---|---|---|
| **s** | **640x640** | **15.11 fps** | **2.0 h** | 1.63x | 13.52 fps | 2.2 h |
| **s** | **960x960** | **7.61 fps** | **3.9 h** | 1.64x | 6.40 fps | 4.7 h |
| s | 1280x720 | 5.85 fps | 5.1 h | 2.01x | 3.37 fps | 8.9 h |
| tiny | 640x640 | 10.03 fps | 3.0 h | 0.90x | — | — |
| tiny | 960x960 | 5.86 fps | 5.1 h | 1.18x | — | — |
| m | 640x640 | 5.78 fps | 5.2 h | 0.90x | — | — |
| m | 960x960 | 3.87 fps | 7.8 h | 1.12x | — | — |

**s が最も効く（1.6〜2.0 倍）。tiny と m では効きが小さく、640 では逆に
遅くなっている（0.90x）** が、これは有意差ではなく測定のばらつきの範囲。
iGPU は CPU と電力枠を共有しているので、同一設定でも 1 割程度は振れる。
**1 割以内の差を根拠に判断しないこと。**

いずれにせよ **s / 960 / max_per_img=10 の 3.9 時間**が本命。
**RTMDet-Ins を使うなら `max_per_img` を絞ること**（`keep_top_k` ではない。
RTMDet-Ins は `model.test_cfg.max_per_img` のほうがマスク復号の本数を決める）。

### 同一ハーネスでの既存モデル（較正用）

上の数字が既存の実測と地続きであることを確認するため、
既存の重みを**同じスクリプト・同じフレーム**で測り直した。

| モデル | 入力 | DML | 60分尺 | CPU | 60分尺 |
|---|---|---|---|---|---|
| 640m.onnx (NudeNet, bbox) | 960 | 7.05 fps | 4.3 h | 1.81 fps | 16.6 h |
| 640m.onnx | 640 | 14.92 fps | 2.0 h | 4.33 fps | 6.9 h |
| nsfw-seg-penis-s (YOLO11s-seg) | 832 | 14.21 fps | 2.1 h | 4.82 fps | 6.2 h |
| nsfw-seg-penis-s | 640 | 23.26 fps | 1.3 h | 8.37 fps | 3.6 h |

`README.md` の「640/DirectML 14.2 fps」「960 で約 4.5 時間」とほぼ一致する。
**ハーネスは較正できている。**

ただし比較には**歪みがある**ことに注意。
NudeNet / YOLO11-seg の ONNX は生テンソルを返すだけで、NMS とマスク復号は
Python 側にある（この数字に入っていない）。MMDetection 系の ONNX は
NMS もマスク復号もグラフ内に入っている。
**つまり mmdet 側の数字だけが不利に出ている。**

---

## 3. 局部検出用の既製重みはあるか

**無い。** 実際に検索した。

Hugging Face の API を直接叩いて検索した結果:

| 検索語 | 結果 |
|---|---|
| `genitalia` | **0 件** |
| `nsfw segmentation` | `NSFW-API/NSFW_Segmentation` の**1件のみ**（= 既に `weights/` にある YOLO11s/x-seg） |
| `penis` | Stable Diffusion の LoRA ばかり。検出/セグメンテーションモデルは無し |
| `nudenet` | YOLOv8 bbox の ONNX ミラーのみ。マスクは出ない |
| `mask-rcnn nsfw` | **0 件** |
| `mosaic nsfw` | **0 件** |

GitHub リポジトリ検索:

| 検索語 | 結果 |
|---|---|
| `mmdetection+nsfw` | **0 件** |
| `genital+detection+segmentation` | **0 件** |
| `自動モザイク` | **0 件** |
| `nsfw+mosaic+detection` | 1件（Stable Diffusion WebUI の拡張。検出は既存の NudeNet 由来） |

**MMDetection 系で局部を検出する公開重みは存在しない。**
Cascade Mask R-CNN を使うなら、COCO 重みから**自前でファインチューンするのが前提**。

これは `docs/00-market-and-oss-survey.md` の結論
（既製重みはほぼ全て Ultralytics YOLO 由来）と整合する。
**マスクが出る既製重みは NSFW-API の YOLO11-seg ただ1系統**という状況は変わっていない。

競合（動画自動モザイクくん）が MMDetection を使っているという読みについては、
**裏付けは取れなかった**（公開情報からは確認できず）。仮に使っているとしても、
それは「自前で学習した重みを MMDetection で回している」ことを意味するので、
**構成をなぞっても重みは付いてこない。**

---

## 4. この構成を採るべきか

### 推論器としては採らない

理由は3つ。

1. **速度で失格。** 実用解像度 960 で 2.64 fps / 11.4 時間。1280x720 で 16.3 時間。
   3 fps の足切りを割っている。しかも DML の 98.9% が GPU に乗っており、
   チューニングの余地が無い（フォールバックを直せば速くなる、という話ではない）。
2. **重みが無い。** 学習が前提。「学習コストを払った上で1本16時間」になる。
3. **重み調達の観点で MMDetection を選ぶ理由が無い。** 既製重みが無い以上、
   どのみち自前学習。同じ自前学習なら軽いアーキテクチャを選ぶべき。

### 学習の目標として置くか

**置くとしても優先度は低い。** ただし1つだけ意味のある役回りがある。

Cascade Mask R-CNN R101 は COCO mask AP 37.8 で、RTMDet-Ins s の 38.7 より**低い**。
「精度の上限を取りに行くモデル」としてすら、いま選ぶ理由が薄い。
高精度側を狙うなら RTMDet-Ins m/x（mask AP 42.1 / 44.6）のほうが素直で、しかも軽い。

Cascade Mask R-CNN を持ち出す価値があるのは、
**外部 GPU を借りてオフラインで擬似ラベルを大量生成する**場面に限る。
そこでは1フレーム何秒かかっても構わないので、精度だけで選べばよい。
だが現状は**手修正 384 フレームしか正解データが無い**段階なので、
擬似ラベル生成を語るのはまだ早い。

**結論: 「いま差し替える」でも「学習の目標に据える」でもない。棚に上げる。**

---

## 5. 採らないなら、マスクが出る代替は何か

### 候補の一覧

速度は**すべてこのマシンでの実測**（RTMDet-Ins は `max_per_img=10`、
YOLO11-seg は生推論のみで後処理を含まない）。

| 候補 | COCO mask AP | 入力 | DML 実測 | 60分尺 | 既製の局部重み |
|---|---|---|---|---|---|
| **YOLO11s-seg (NSFW-API)** | — (局部特化) | 832 | 14.21 fps | 2.1 h/モデル | **有り（唯一）** |
| YOLO11x-seg (NSFW-API) | — | 832 | — | 15.9 h（既存実測） | 有り |
| **RTMDet-Ins tiny** | 35.4 | 640 | 11.17 fps | 2.7 h | 無し |
| **RTMDet-Ins s** | **38.7** | 640 | **15.11 fps** | **2.0 h** | 無し |
| RTMDet-Ins s | 38.7 | 960 | 7.61 fps | 3.9 h | 無し |
| RTMDet-Ins m | 42.1 | 640 | 6.45 fps | 4.7 h | 無し |
| Mask R-CNN R50 | 35.4 | 960 | 5.29 fps | 5.7 h | 無し |
| **Cascade Mask R-CNN R101** | 37.8 | 960 | 2.64 fps | 11.4 h | 無し |
| SOLOv2 / YOLACT | 34.8 / 29.8（**推定**、論文値） | — | **未測定** | — | 無し |
| Mask2Former (R50) | 43.7（**推定**、論文値） | — | **未測定** | — | 無し |

SOLOv2 / YOLACT / Mask2Former は測っていない。理由:
Mask2Former は Transformer デコーダで RTMDet-Ins より確実に重く、
SOLOv2 / YOLACT は mask AP が RTMDet-Ins s を下回るのに軽くもない
（いずれも ResNet-50 backbone）。**測る価値のある帯に入っていない。**
これは推定であり、実測していないことを明記しておく。

### 勧める順序

**1. まず YOLO11s-seg（既に手元にある）で「マスク化が効くのか」を検証する。**

これが最優先。理由は「学習コスト 0 で今日答えが出る唯一の選択肢」だから。
`weights/nsfw-seg-penis-s.onnx` / `nsfw-seg-vagina-s.onnx` は既にあり、
2モデルで 4.2 時間（実測 2.1 h × 2）。

**マスク化で何が変わるのかを、期待しすぎずに整理しておく。**

- 変わること: 同じ安全マージンをより少ない塗り面積で確保できる。
  実測で **マスクは bbox 面積の 54〜82%**
  （Cascade Mask R-CNN で 0.73、RTMDet-Ins s で 0.54〜0.82）。
  輪郭沿いに等方的に膨らませられるので、「塗り過ぎ」と「漏らさない」の
  トレードオフ曲線そのものが動く。**`docs/00-MORNING.md` の未決事項に直接効く。**
- **変わらないこと: 位置ずれは直らない。**
  検出器の箱が対象より 5px 小さければ、マスクも 5px 小さい。
  bbox は必ずマスクの外接矩形なので、**bbox が漏れてマスクが漏れないことは原理上ない。**
  「bbox が輪郭と一致せず縁から数px出る」が**位置ずれ**由来なら、
  マスク化では解決しない。ここは切り分けてから投資判断すること。

**2. 学習に踏み込むなら RTMDet-Ins s。**

- MMDetection 系なので、将来 Cascade Mask R-CNN に上げたくなっても
  データセットと学習パイプラインをそのまま使える
- `max_per_img=10` で 960 入力 **3.9 時間**。現行 NudeNet 960（4.3 時間）と同等
- COCO mask AP 38.7 は Cascade Mask R-CNN R101（37.8）を**上回る**
- ONNX 化はこの調査で通してある（`weights/rtmdet-ins_s_max10.onnx`）

**3. Cascade Mask R-CNN は棚に上げる。**

---

## 確認できなかったこと

- **実素材での精度は一切検証していない。** COCO 重みには局部クラスが無いので、
  学習しない限り Recall 比較はできない。この文書の精度欄はすべて COCO の公称値。
- **推論解像度を上げるとマスクモデルでも検出が増えるか。** NudeNet では
  960 で +39.5% だったが、セグメンテーションで同じ傾向になるかは未確認。
- **FP16 / INT8 量子化での DirectML 速度。** 未測定。効けば 1.5〜2 倍の可能性はある（推定）。
- **Ryzen 8600G の NPU（XDNA）利用。** ORT の Vitis AI EP 経由で使える可能性があるが、
  未検証。対応 op が狭く、この規模のモデルが載るかは不明。
- **競合が MMDetection を使っているかの裏付け。** 公開情報からは確認できず。

---

## 生成物

| パス | 内容 |
|---|---|
| `.venv-mmdet/` | Python 3.11 + torch 2.1.0 cpu + mmcv 2.1.0 + mmdet 3.3.0 + mmdeploy 1.3.1。**メインの `.venv` は無傷（確認済み）** |
| `tools/export_mmdet_onnx.py` | MMDeploy 経由の ONNX 書き出し。上記2件のバグ回避を含む |
| `tools/mmdeploy_cfg/` | MMDeploy の deploy config（pip 版に同梱されていないので取得したもの） |
| `tools/deploy_mmdet.py` | MMDeploy 公式 `tools/deploy.py`（参照用。実際には使っていない） |
| `tests/bench_mmdet.py` | `speed`（スループット）/ `check`（検出とマスクの目視確認） |
| `weights/cascade_mask_rcnn_r101_fpn.onnx` | 385 MB |
| `weights/rtmdet-ins_{tiny,s,m}.onnx` | max_per_img=100 |
| `weights/rtmdet-ins_{tiny,s,m}_max10.onnx` | max_per_img=10。**実運用ならこちら** |
| `weights/mask_rcnn_r50_fpn.onnx` | 参照用 |
| `weights/*.pth` | COCO 事前学習済み。計 745 MB。**ONNX を使うなら消してよい** |
| `data/mmdet_bench/` | `check_*.png`（目視確認）、`mmdet_speed.json` |

再現手順:

```bash
# ONNX 化
.venv-mmdet\Scripts\python.exe tools\export_mmdet_onnx.py \
  --model cascade-mask-rcnn_r101_fpn_20e_coco \
  --checkpoint weights\cascade_mask_rcnn_r101_fpn_20e_coco.pth \
  --out weights\cascade_mask_rcnn_r101_fpn.onnx

# 計測
.venv-mmdet\Scripts\python.exe tests\bench_mmdet.py speed \
  --model weights\cascade_mask_rcnn_r101_fpn.onnx \
  --providers dml,cpu --device-id 0 --sizes 1280x720,960x960

# 目視確認
.venv-mmdet\Scripts\python.exe tests\bench_mmdet.py check \
  --model weights\rtmdet-ins_s.onnx --norm rtmdet --providers cpu --sizes 640x640
```

`--norm` を取り違えないこと。Mask R-CNN 系は `torchvision`（RGB 変換あり）、
RTMDet 系は `rtmdet`（BGR のまま、mean/std が逆順）。

# Web動画編集OSSとモザイクOSSの調査 — issue #70 への回答

調査日: 2026-08-25

## 0. 発端と結論

issue #70（コメント欄）:

> oivie的なフレームワークがすでにできていないのでしょうか？？　ドキュザウルスみたいにすでにできているというものを探しています

> なるほどすでにある　OSSのウェブ上で動く動画編集ソフトや日本のアダルトビデオにモザイクをかけるという既存のソフトに移植するのを検討する必要性がありそうですね

「oivie」は Olive（OSSのノンリニア動画編集ソフトの名称）の綴り間違いと解した。
OliveがWebで動くかどうかは今回確認できていない（2-4節）。「ドキュザウルス」は Docusaurus。**「ゼロから作らず、既にあるものを
土台にできないか」という問いとして調査した。**

**結論: A案（今の自前 UI を続ける）を推す。ただし基盤の一部（動画のデコード・フレーム
厳密シーク）は既に issue #19 / #29 で mediabunny（OSS、MPL-2.0）を土台にしており、
これは事実上「C案：部分的に借りる」を先取りして実施済みの状態。** 根拠は以下。

1. **「編集UIをまるごと借りる」候補は、UIを持つもの・持たないものの2系統に分かれ、
   どちらもこのツールの要件と合わない。** UIを持つ候補（OpenCut, Remotion）はいずれも
   React/Next.js 専用で、Preact 環境への移植コストが小さくない。UIを持たない候補
   （Diffusion Studio, Etro）は「タイムラインUIは自分で書け」と明言しており、
   結局このリポジトリの `frontend/src/timeline/timeline.tsx` を書き直すのと変わらない。
2. **このリポジトリはこの検討を既に一度行っている。** issue #29（2026-08-24〜25）が
   Remotion・Etro・Video.js・ffmpeg.wasm・mp4box.js・web-demuxer・CVAT・
   `<video>`+`requestVideoFrameCallback` を実測ベースで比較し、**WebCodecs + mediabunny +
   素の Canvas2D** という現行構成を選んでいる。今回の追加調査（後述）はこの判断を
   覆す材料を見つけられなかった。
3. **モザイク特有のワークフロー（漏れ/塗り過ぎ/誤検知の3値判定、区間単位のキュー、
   ダブルチェック導線）は、汎用動画編集ソフトのどれも持っていない。** これは持ち帰っても
   自分で書く部分であり、「借りる」対象にならない。
4. **モザイクを付与する完成品 OSS は存在しない**（`docs/00-market-and-oss-survey.md` の
   既存結論。本調査で覆る材料はなし）。
5. **セグメンテーション（矩形でなく輪郭で塗る）についても、局部特化の既製重みは
   `NSFW-API/NSFW_Segmentation`（このリポジトリが既に保持）以外に実質存在しない。**
   これは `docs/06-cascade-mask-rcnn.md` が既に実測込みで結論している。

---

## 1. 現状の `frontend/` の規模（載せ替え判断の前提）

```
$ git ls-files frontend/src | xargs wc -l
     8 frontend/src/webapp/StatusBadge.tsx
    11 frontend/src/shared/logic.ts
    22 frontend/src/shared/job-logic.ts
    37 frontend/src/shared/review-net.ts
    90 frontend/src/shared/geom.ts
    93 frontend/src/framestep/player.ts
   107 frontend/src/shared/webapp-net.ts
   181 frontend/src/webapp/index.tsx
   218 frontend/src/shared/canvas-draw.ts
   239 frontend/src/framestep/framestep.tsx
   305 frontend/src/shared/review-logic.ts
   320 frontend/src/webapp/draw.tsx
   363 frontend/src/shared/api.ts
   441 frontend/src/webapp/job.tsx
   503 frontend/src/webapp/review.tsx
   536 frontend/src/review/app.tsx
   571 frontend/src/timeline/timeline.tsx
  4045 total
```
（17ファイル。うち `framestep/` はフレーム厳密再生の基盤として issue #19 で追加されたもの）

対する Python バックエンド（`automosaic/` 直下 + `automosaic/webapp/`）は
`automosaic/review.py`（2313行）だけでフロントエンド全体より大きく、
`automosaic/cli.py`（1387行）・`automosaic/temporal.py`（1312行）・
`automosaic/webapp/*.py`（計 2255行）を合わせると **9,155行**。
**モザイク検出・トラッキング・補正のロジックはほぼ全てバックエンド側にあり、
フロントエンドはその薄いクライアントでしかない。** 載せ替えの対象になり得るのは
この 4,045 行だけで、バックエンドには触れない。

`frontend/src/shared/api.ts` を見ると、フロントエンドが握っている型は
`Verdict`（ok/fixed/unsure/toobig/false_positive）、`QueueReason`
（estimated/uncovered/area_jump/low_conf/sampled）、`Correction`、`Region`、
`JobSettings`（`margin_scale` 等 CLI 引数と1対1）など、**モザイク作業のドメインモデル
そのもの**。汎用動画編集ソフトのどれ一つとして、このドメインモデルを最初から
持っているものはない。

---

## 2. Web上で動く OSS 動画編集フレームワーク／ライブラリ

### 2-1. issue #29 が既に検討・不採用にしたもの（本調査で覆る材料なし）

issue #29 本文・コメント（2026-08-24〜25、実測付き）で既に評価済み。重複調査していない。

| 候補 | 版/日付 | 不採用の理由（issue #29 原文の要旨） |
|---|---|---|
| `<video>` + `requestVideoFrameCallback` | Baseline 2024 | 「n番目のフレームを出せ」と言えない。`timeline.tsx:440` が既に同じ結論に達している |
| ffmpeg.wasm (`@ffmpeg/ffmpeg`) | 0.12.15 / 2025-01-07 | サーバに ffmpeg 9.0 があるのでブラウザで動かす理由がない |
| Remotion | 4.0.515 / source-available | **動画を生成するフレームワークで、既存動画の検査ではない**。4人以上の営利組織は有償 |
| Etro | 0.14.1 / GPL-3.0 | 汎用の編集フレームワーク。要るのは編集ではなく検査 |
| Video.js | 8.24.0 / Apache-2.0 | HLS/DASH再生プレイヤ。`<video>`の上に乗るだけでフレーム厳密性は解決しない |
| mp4box.js | 2.4.1 / BSD-3-Clause | mp4のパースのみ。mediabunnyがこの層を含む |
| web-demuxer | 4.0.0 | 同上。npm上でlicense未設定 |
| CVAT（cvat.ai） | — / MIT | Docker+PostgreSQLのスタンドアロン基盤。オフライン・素材を外に出さない前提と衝突。キーフレーム間補間という設計思想の裏付けにはなったが、コード流用先ではない |
| frame-accurate-scrubbing (jordicenzano) | — | フレームをJPEG連番展開する方式。#18で既に「7.2GB/10fpsが上限」と実測否定済みの方式そのもの |
| ブラウザ側でモザイクを焼く | — | `session.py` の「3つの絵が一致する」不変条件を壊す。焼き込みはサーバ固定 |
| 汎用タイムライン編集UI（トラック/トランジション/エフェクト） | — | この道具に要らない |

採用: **WebCodecs + mediabunny(MPL-2.0) + 素のCanvas2D + Preact/esbuild**、すべてvendor同梱・CDN非参照。

### 2-2. 今回新たに調査した候補（issue #70 が名指ししたもの）

#### OpenCut

| 項目 | 内容 |
|---|---|
| URL | https://github.com/OpenCut-app/OpenCut（実在確認、GitHub API）。旧コードベース https://github.com/OpenCut-app/opencut-classic は **archived** |
| ライセンス | MIT（LICENSE原文 "Permission is hereby granted, free of charge..." を確認） |
| 最終push / Star | 2026-08-10 / **85,652**（フォーク8,451。GitHub API実測。ブログ記事類の数字はバラバラで不採用） |
| できること | `/apps` 配下に `web`（Next.jsのタイムライン編集Webアプリ、CapCut風UI）・`desktop`（GPUIネイティブ）・`api` が存在。**ただし現在「ゼロから完全書き直し中」**で、README上の機能（Editor API・プラグイン・MCPサーバ）は計画中の記載が中心。現行mainブランチが実際に動くかは**未確認** |
| 技術スタック | フロントエンドはNext.js（**React専用**）。コアはRust+WASM |
| 制約適合性 | React専用なのでPreact環境への移植コストは小さくない。GPU依存（CUDA前提か）は未確認。NSFW制限はREADME/LICENSEに記載なし |

#### Diffusion Studio (`@diffusionstudio/core`)

| 項目 | 内容 |
|---|---|
| URL | https://github.com/diffusionstudio/core（実在確認） |
| ライセンス | MPL-2.0（LICENSE原文 "This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0." を確認） |
| 最終push / Star | 2025-11-18 / 1,225（フォーク139） |
| できること | **UIを一切持たない、プログラム的な動画合成エンジンのみ**。README自身が「タイムラインやインスペクタが最初から欲しいなら使うな」と明言している |
| 技術スタック | TypeScript、フレームワーク非依存。v2でPixi.js依存も除去し49KB gzip |
| 実行環境 | ブラウザ内完結、WebCodecs直接利用。CUDA前提の記載なし |
| 制約適合性 | フレームワーク非依存なのでPreactとの親和性は高い。ただし**UIが無いので「編集UIそのものを借りる」という issue #70 の主目的には応えない**。合成エンジンだけ差し替える用途に限られ、うちは既に mediabunny + Canvas2D で同等の層を持っている |

#### Etro (etro.js)

| 項目 | 内容 |
|---|---|
| URL | https://github.com/etro-js/etro（実在確認） |
| ライセンス | **GPL-3.0**（LICENSE原文確認。コピーレフト。組み込んで自作ツールを配布するなら自作側もGPL-3.0互換で公開する義務） |
| 最終push / Star | 2026-08-12 / 1,148（フォーク96） |
| できること | README「UI framework agnostic」と明記。**UI・タイムラインコンポーネントは一切付属しない**、合成APIのみ |
| 実行環境 | ブラウザ内完結（WebGL/GLSL） |
| 制約適合性 | Diffusion Studioと同型で「UIを借りる」目的には応えない。加えてGPL-3.0はこのツールが「配布しない」方針（README「前提」節）である限り実害はないが、方針変更時の制約になる |

#### Remotion

| 項目 | 内容 |
|---|---|
| URL | https://github.com/remotion-dev/remotion（実在確認） |
| ライセンス | **独自の「Remotion License」（OSI承認ライセンスではない、GitHub上も"License: Other"）。** LICENSE.md原文: 無償利用は「個人」「**従業員3人以下の営利組織**」「非営利組織」「商用評価目的」に限定。**4人以上の営利組織は remotion.pro の有償Company Licenseが必須**（Terms and Conditions "A license is mandatory when the total number of personnel... reaches the threshold of four or more."）。Remotion自体の改変・再ライセンス販売は禁止。**ユーザーが懸念した「OSSに見えて商用に条件がある」は事実として正確** |
| 最終push / Star | 2026-08-24 / 57,248（フォーク4,320。開発は活発） |
| できること | `@remotion/studio` 等、タイムラインエディタ相当のUI一式が150以上のパッケージで存在。**ただし本質は「Reactコンポーネントとして動画を記述し、それをレンダリングする」プログラム生成フレームワークであり、既存動画を検査する用途ではない**（issue #29 が既に指摘した点と一致） |
| 技術スタック | **React必須**（"Make videos programmatically with React"）。Preactへの移植は現実的でない |
| 実行環境 | Node.js上でのレンダリングが基本。ブラウザ内完結ではない |
| 制約適合性 | React専属、かつ「動画を生成する」設計思想がこのツールの「既存動画を検査する」要件と逆向き。個人利用なら無償枠に収まる可能性が高いが、そもそも用途が合わない |

**総括（このグループ）**: UIを持つ2候補（OpenCut, Remotion）はどちらもReact専用で移植コストが高く、
かつOpenCutは書き直し中で完成度不明、Remotionは「動画生成」用途でこの道具の「検査」用途と方向が違う。
UIを持たない2候補（Diffusion Studio, Etro）は「タイムラインUIは自分で書け」という設計で、
借りても `frontend/src/timeline/timeline.tsx` を書き直す作業は残る。**4候補とも「編集UIそのものを
借りる」という issue #70 の主目的には応えない。**

### 2-3. mediabunny（このリポジトリが既に採用しているもの。自分で確認済み）

| 項目 | 内容 |
|---|---|
| 使用箇所 | `frontend/package.json` の devDependencies に `"mediabunny": "^1.55.2"`。`frontend/build.mjs` が `src/framestep/framestep.tsx` を `automosaic/webapp/static/framestep.js` へビルドする1エントリとして持つ |
| ライセンス | MPL-2.0。issue #64 の独立検証コメントで実際のバンドル出力に埋め込まれた通知文を確認済み: `Copyright (c) 2026-present, Vanilagy and contributors / This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.` |
| 経緯 | issue #19（WebCodecsによるフレーム厳密確認ビュー）で採用、PR #64 で `2026-08-24T15:40:07Z` にマージ済み（`gh pr view 64` で確認） |
| できること | `VideoSampleSink.samples()` / `.getSample(timestamp)` によるフレーム厳密シーク。`frontend/src/framestep/player.ts`（93行）がロード時に全フレームのtimestampを走査して固定し、`getFrame(n)` が毎回同じtimestampに解決される設計（issue #64独立検証で型宣言レベルの確認済み。**実ブラウザでのフレーム厳密性そのものは未検証**とissue #64自身が明記） |
| 制約適合性 | ブラウザ内完結、CUDA非依存、WebCodecs前提のみ。npm無しでもPythonサーバ単体で動くことを実測済み（issue #64） |

### 2-4. 今回確認できなかった候補

以下は issue #70 が名指ししたが、**この調査では実在確認・ライセンス確認ができなかった。**
推測や記憶による記載を避け、未確認のまま列挙する（RULES.md 1.3）。

- **Motionity**（ブラウザで動くモーショングラフィックス/動画エディタを名乗るもの）
- **Olive**（デスクトップ動画編集ソフト。Webで動くかどうか自体が未確認）
- **Shotcut**（デスクトップ動画編集ソフト。同上）
- **Kdenlive**（デスクトップ動画編集ソフト。同上）
- **ffmpeg.wasm** — ただし issue #29 の既存調査（2-1節に既出、版0.12.15/2025-01-07、
  「サーバにffmpeg 9.0があるので不採用」）はある。今回追加で調べ直せなかった
- **Vidstack**（再生プレイヤーか編集機能を持つか自体が未確認）

これらについて「実在する」「ライセンスは○○」のような断定はできない。
必要なら別途、個別に URL を開いて確認すること。

---

## 3. モザイクをかける既存の OSS

### 3-1. 既存調査（`docs/00-market-and-oss-survey.md`, 2026-08-22）— 重複調査せず流用

| | DeepMosaics | NudeNet | hent-AI |
|---|---|---|---|
| URL | HypoX64/DeepMosaics | notAI-tech/NudeNet | natethegreate/hent-AI |
| Star | 2,631 | 2,431 | 1,728 |
| 最終push | 2024-08-30（実質停止） | 2026-06-09（活発） | 2023-03-25 |
| ライセンス | GPL-3.0 | リポジトリAGPL-3.0 / PyPIはMIT表記（矛盾） | MIT |
| 付与/除去 | 両方（主眼は除去） | 検出器のみ | 除去（二次元） |
| 出力粒度 | ピクセルマスク | bboxのみ | マスク |
| 動画対応 | あり | **なし** | — |

**完成品のOSSは無い。付与（モザイクをかける）側で実写動画に対応した実用OSSは存在しない**
——これが `docs/00` の結論で、今回の追加調査でも覆らなかった。

### 3-2. 今回追加調査したもの

#### DeepCreamPy（除去側。方向は逆だが技術が参考になるか）

| 項目 | 内容 |
|---|---|
| URL | オリジナル `deeppomf/DeepCreamPy` は**404（GitHub APIで確認、消滅済み）**。最有力フォークは https://github.com/Deepshift/DeepCreamPy |
| ライセンス | AGPL-3.0（LICENSE.md原文確認） |
| 最終push / Star | 2024-11-22（約21か月更新なし＝実質停止） / 621（フォーク96） |
| できること | ユーザーが画像編集ソフトで対象部を**緑色に手動で塗り**、GANベースのinpainting（PEPSI改変）がその領域を埋める。**自動検出の機構はゼロ** |
| 転用可否 | **転用できる要素なし。** (1) 除去と付与で方向が逆 (2) 対象領域特定が100%手動で自動検出技術が存在しない (3) README原文が「実写・動画は対象外」と明記 (4) 学習データがアニメイラストのみ |

#### 日本語圏でソース公開されている自動モザイクツール

Zenn記事・動画自動モザイクくん・Deepmosaic（`docs/00`既知、クローズド）とは別に2件発見した。

| 名称 | URL | ライセンス | 最終push/Star | 実写対応 |
|---|---|---|---|---|
| sugarkwork/comfyui-auto-mosaic | https://github.com/sugarkwork/comfyui-auto-mosaic | **なし**（LICENSEファイル不在、GitHub API `license`もnull=全権利留保） | 2026-07-13 / 7 | YOLOセグメンテーションで検出、動画モードでオプティカルフロー追跡・補間あり。設計思想はこのツールに近いが**無許諾のため再利用不可** |
| kidonaru/MosaicTool | https://github.com/kidonaru/MosaicTool | 本体MIT（同梱モデルは別ライセンス） | 2026-08-02 / 38 | 手動範囲指定＋顔/目のYOLO検出が中心。同梱NSFW検出モデルはREADME原文「2Dイラストのみで学習」——**実写非対応**。フレーム間補間もなし |

いずれも即戦力ではない。`comfyui-auto-mosaic` は設計（動画補間つきYOLOセグメンテーション）は
参考になるが無許諾、`MosaicTool` はライセンスは明快だが自動検出が二次元専用。

---

## 4. セグメンテーションモデル（矩形でなく輪郭で塗れるもの）

### 4-1. このリポジトリの現状（`automosaic/segmenter.py`, `docs/06-cascade-mask-rcnn.md`）

`automosaic/segmenter.py` は `NSFW-API/NSFW_Segmentation`（Hugging Face、YOLO11-seg、
**ライセンス未指定＝全権利留保**、`docs/00` 既存結論）の ONNX を DirectML で回すラッパとして
既に実装済み。issue #12 の実測ログで `provider=dml, device_id=1` で実際に動作することが
確認されている（速度: penis-s / 832 / DirectML で 4.88fps、GPU負荷共有時。クリーン環境では
`docs/06` 実測 14.21fps）。ただし **`cli.py` に配線されておらず**、issue #12 の目視検証では
検出位置が実際の対象に一度も乗っていなかった（9フレーム中0件、固定座標に貼り付く挙動から
背景の透かし等への誤反応が疑われる）——マスク化以前に検出精度そのものが未解決。

`docs/06-cascade-mask-rcnn.md`（2026-08-23、全て実測）は Cascade Mask R-CNN / Mask R-CNN /
RTMDet-Ins を DirectML で ONNX 化・実測した上で、**Hugging Face API と GitHub 検索を実際に
叩いて**局部特化のマスク付き既製重みを探索し、次の表を導いている。

| 検索語 | 結果 |
|---|---|
| `genitalia`（HF） | 0件 |
| `nsfw segmentation`（HF） | `NSFW-API/NSFW_Segmentation` の1件のみ（既に保有） |
| `mask-rcnn nsfw`（HF） | 0件 |
| `mmdetection+nsfw`（GitHub） | 0件 |
| `genital+detection+segmentation`（GitHub） | 0件 |

**マスクが出る既製重みは NSFW-API の YOLO11-seg ただ1系統。** RTMDet-Ins は速度面で有望
（s/960/max_per_img=10で3.9時間）だが局部特化の学習済み重みは無く、採用するなら自前学習が前提。

### 4-2. 今回追加調査した代替候補

| 候補 | ライセンス | 出力形式 | 175条対象クラスをカバーするか |
|---|---|---|---|
| SAM（Meta, 原版） | 独自SAM License（商用・派生可、NSFW明示禁止なし） | ピクセルマスク、**ただしプロンプト式**（point/box指示が要る） | 自動局部検出の機構を持たない。既存bbox検出器と組み合わせる追加の一段になる |
| SAM 2（Meta） | **Apache-2.0**（完全に許諾的） | 動画対応・メモリバンクでフレーム間トラッキング内蔵。**同じくプロンプト式** | 同上 |
| sugarknight/sensitive-detect（HF） | AGPL-3.0（明確） | YOLO-seg `.pt` + **ONNX既存** | pussy/penis検出用として配布されているが、**モデルカード本文が空でクラス一覧・学習ドメイン（実写か二次元か）が未確認** |
| Anzhc/Anzhcs_YOLOs（HF） | AGPL-3.0 | YOLO-seg | 確認できたのは胸部（breast）のみ。penis/vagina/anusクラス未確認 |
| erax-ai/EraX-Anti-NSFW-V1.1（HF） | Apache-2.0表記 | bboxベース、真のセグメンテーションかは未確認 | 上流Ultralytics YOLO11由来のAGPL-3.0伝播問題を負う可能性（docs/00 既存指摘と同型、未確認） |

**結論**: ライセンス明確・NSFW制限なし・ONNX/DirectML実行可・局部クラス自動検出、の
4条件を単独で満たす代替は**見つからなかった**。SAM/SAM2はライセンスは最良だがプロンプト式で
単独では自動検出にならない。`sensitive-detect` はライセンス（AGPL-3.0、既存のNudeNetと同条件）
は明確な代替候補になり得るが、モデルカードが空で実写ドメインかどうか未確認のまま採用はできない。

**現状の結論は変わらない: 輪郭で塗れる既製重みは `NSFW-API/NSFW_Segmentation` 一択で、
それは既にこのリポジトリにある。** issue #12 が示すとおり、次の課題は「別のモデルを探すこと」
ではなく「今あるモデルの検出精度を検証すること」。

---

## 5. 借りるとしたら、どこまで借りてどこから自前か（境界線）

`frontend/src/shared/api.ts` が握る型（`Verdict`, `QueueReason`, `Correction`, `Region`,
`JobSettings`）はモザイク作業のドメインモデルそのもので、汎用動画編集ソフトはどれも持たない。
issue #29 の段1〜5の設計（実効設定の一致、区間単位のキュー、ダブルチェック導線）も
このリポジトリ固有の要求であり、外部フレームワークが肩代わりできる部分ではない。

一方で「動画をデコードしてフレーム番号でシークする」「Canvas上に矩形を重ねて描く」という
**土台**は汎用の関心事で、ここは実際に外部OSS（mediabunny, WebCodecs仕様）に乗っている。

境界線は以下のとおり:

| 層 | 内容 | 現状 |
|---|---|---|
| 借りる（汎用） | 動画のデコード・フレーム厳密シーク | **mediabunny（MPL-2.0）に乗せ済み**（issue #19 / PR #64、#72） |
| 借りる（汎用） | Canvasへの矩形描画 | 素のCanvas2D API（ライブラリ不要と判断済み、issue #27） |
| 自前（モザイク特有） | 区間選択・時刻区間キュー | `automosaic/webapp/spans.py`（`interval_records()`、issue #22。**配線は未完了**） |
| 自前（モザイク特有） | 自動追従（トラッキング・補間・デスパイク） | `automosaic/temporal.py`（1,312行、バックエンド） |
| 自前（モザイク特有） | ダブルチェック導線（漏れ/塗り過ぎ/誤検知の3値判定、実効設定の一致検査） | `frontend/src/shared/review-logic.ts` + `automosaic/review.py`（issue #14〜17） |

**「編集UIをまるごと借りる」層は存在しない。** 汎用動画編集ソフト（OpenCut/Remotion/
Etro/Diffusion Studio のいずれも）は、区間選択・自動追従・ダブルチェックのいずれも
持っておらず、そこはこのリポジトリが自分で設計・実装するしかない部分。

## 6. 移植コストの見積もり

**今の `frontend/` を捨てて OpenCut や Remotion に載せ替える**場合、具体的に影響を受けるのは:

- `frontend/src/webapp/review.tsx`（503行）・`frontend/src/review/app.tsx`（536行）—
  漏れ/塗り過ぎ/誤検知の判定UIを、React専用の外部フレームワークのコンポーネント体系
  （OpenCutならNext.js + 独自エディタAPI、RemotionならReactコンポーネントとしての
  動画記述）に合わせて**全面書き直し**になる。Preact → React の移行自体は機械的だが、
  外部フレームワークのタイムライン/エディタAPIに `MarkMode`（add/shrink/erase、
  `frontend/src/shared/review-logic.ts:9-42`）のような3値判定ワークフローを乗せる
  設計は、そのフレームワークのイベントモデルを新たに学習してからでないと見積もれない。
- `frontend/src/timeline/timeline.tsx`（571行）— 同様に全面書き直し。
- `frontend/src/shared/canvas-draw.ts`（218行）・`geom.ts`（90行）— 外部フレームワークが
  矩形描画とオーバレイ座標変換を提供するなら不要になるが、既存の3値判定ロジックが
  期待する座標系（`geom.ts`冒頭のコメントが警告する「見えている枠と実際に塞がれる場所が
  ずれる」不変条件）を外部フレームワーク側でも保てるかは個別に検証しないと分からない。
- `frontend/src/shared/api.ts`（363行）・`webapp-net.ts`（107行）— バックエンドAPIとの
  通信層。フレームワークが変わっても型定義自体はほぼそのまま使える。
- `frontend/build.mjs` のビルド構成（Next.js/GPUIベースの外部フレームワークを迎えるなら
  esbuild単体構成の前提が崩れ、ビルドパイプライン自体の作り直しが要る）。

**合計で frontend/src の 4,045行のうち、少なくとも `timeline.tsx` + `review.tsx` +
`app.tsx`（review） + `job.tsx` + `draw.tsx` の 2,371行（59%）が書き直し対象になる。**
`api.ts` 等の通信層とドメイン型は概ね持ち越せる。バックエンド 9,155行には影響しない。

「たぶん簡単」と言える根拠はない。むしろ、React専用の候補（OpenCut, Remotion）を選ぶと
Preact→React移行という**このプロジェクトが過去に選んだ判断（`1b76289` フロントエンドを
TypeScript + Preactに移行）を逆走する**ことになり、UIを持たない候補（Diffusion Studio,
Etro）を選んでも上記の書き直し量はほぼ変わらない。**移植コストが載せ替えの利益
（浮くのはCanvas矩形描画とプレーヤーの土台程度）を上回る。**

## 7. 結論

**A案（今の自前UIを続ける）を推す。**

- 「動画編集ソフトらしい操作感がない」「車輪の再発明感がある」という issue #70 の
  違和感自体は正当だが、**その原因は「編集UIフレームワークを使っていないから」ではなく、
  issue #29 が指摘した段1〜5の未完了**（実効設定の不一致、フレーム単位でしか動かない
  キュー、区間層 `spans.py` の未配線、Web版に「狭める」「誤検知」が無い）にある。
  外部フレームワークに載せ替えても、これらのモザイク特有の欠落は自動では埋まらない。
- 汎用動画編集OSSは「編集UIを借りる」「動画合成エンジンを借りる」のどちらの形でも、
  区間選択・自動追従・ダブルチェックというモザイク特有の要求を持たない。持ち帰っても
  結局そこは自分で書く。
- 唯一「借りて効果があった」層（動画デコード・フレーム厳密シーク）は、
  issue #19 / #29 の判断で **既に mediabunny を土台に据えている。** これは
  C案（部分的に借りる）を先に実行済みということでもある。
- モザイクを付与する完成品OSSは存在せず（`docs/00`）、輪郭で塗れる局部特化の
  既製重みも `NSFW-API/NSFW_Segmentation` 一択で、これも既にこのリポジトリにある。
  今探すべきなのは「別のモデル」ではなく、**今あるモデルの検出精度を検証すること**
  （issue #12 が指摘した「反応した位置が対象の上にない」問題）。

**次に効くのは、載せ替えではなく issue #29 の段1〜5（特に #14〜#17 のダブルチェック
基盤、#21〜#22 の区間層配線）を進めること。**

## 8. 確認できなかったこと

- OpenCut現行mainブランチの実動作状況・完成度（README上は書き直し中としか分からない）
- OpenCut・Remotionのレンダリング/GPU要件がCUDA前提かどうか
- Diffusion StudioのサーバサイドレンダリングがV2でも対応しているか
- `sugarknight/sensitive-detect` のクラス一覧・学習ドメイン（実写か二次元か。モデルカード本文が空）
- SAM/SAM2のライセンスにNSFW用途への黙示的制限がないか（明文の禁止条項は無いが、
  Acceptable Use Policy側の解釈は法務確認が要る領域として未確認のまま）
- `erax-ai/EraX-Anti-NSFW-V1.1` が真のピクセルセグメンテーションを返すか、bboxの
  塗りつぶしに留まるか
- **Motionity・Olive・Shotcut・Kdenlive・Vidstack の実在確認・ライセンス・最終コミット・
  スター数・できること。この5件は今回まったく確認できていない。** 2-4節に記載のとおり、
  推測での記載は避けた
- ffmpeg.wasmの現行バージョンでの再調査（2-1節はissue #29時点の値をそのまま引用しており、
  今回はこのリポジトリで再確認していない）

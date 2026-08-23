# 次の周でやること

`docs/07-audit-2026-08-23.md` の修正周では扱わないと決めたもの。
基準点をコミットしてから、1件ずつ独立して戻せる形で足す。

---

## 解決済み: 回転メタデータで全フレームが壊れる

issue #1 / PR #20 で修正済み（`9691618`）。独立検証で 90/180/270/回転なし の4通りを
画素と座標で確認した。180度の上下反転も起きない（平均差分 0.2 台。反転していれば 238）。

**故障クラスは残っている。** パス2は流れてくるフレームの寸法を一度も測らず、
`y_size = w*h` が w と h の入れ替えに対して不変なので長さ検査が素通りする。
別経路で probe と ffmpeg がずれれば同じ故障が復活する。issue #32 に切った。

**運用への影響**: 回転素材について、修正前に作った検出JSON・チェックポイントは
今後 exit 1 で拒否される（解像度が食い違うため）。取り直しになる。

<details><summary>当時の調査（記録として残す）</summary>


**例外も警告も出さず、exit 0 で「完成品」が出る。スマホの縦撮りが該当する。**

```
ffmpeg -display_rotation 90 -i base360.mp4 -c copy rot2.mp4
ffprobe stream=width,height  -> 640,360      # probe() が読む値
ffprobe stream_side_data     -> rotation=90
ffmpeg -i rot2.mp4 ... png   -> (360, 640)   # 実際にデコードされるサイズ
```

`video.py` の `probe()` が `side_data_list` を見ないので `info.width=640, height=360`。
`open_full_reader` は自動回転して 360x640 を流す。**バイト数は 640*360 == 360*640 で
一致するため `FrameBuffer` の長さ検査を素通りし、reshape だけが転置される。**
出力フレームは斜めに裂けた縞になる。

さらに `cli.py` の `scale_back = info.width / dec_w` が 640/540 = 1.185（正しくは
360/540 = 0.667）になるので、パス1の検出座標自体が 1.78 倍ずれて別の場所に載る。

監査 C-1 と同型だが、C-1 より発生条件が日常的。Web からも同じ経路に入る。

直し方: `probe()` で `side_data_list` の `rotation` を読み、90/270 なら width/height を
入れ替える。または `-noautorotate` を付けて元の向きで扱う。

</details>

## 奇数解像度がパス2で落ちる（監査「その他」）

`--detect-only` は通るので、検出に数時間かけた後に描画で落ちる。

VP9/WebM は 4:2:0 のまま 641x481 を持てる（H.264 4:2:0 は不可能で x264 は黙って偶数に丸める）。
MJPEG(4:4:4)、H.264 4:4:4 も奇数可。

壁が2枚ある。

1. `render.py` の `FrameBuffer.__init__` の guard が `ValueError`
2. **guard を外しても直らない。** ffmpeg の彩度平面は `ceil(w/2) x ceil(h/2)`、
   `FrameBuffer` は `width//2`（切り捨て）。実測の差:

   | | ffmpeg が出すバイト数/frame | `FrameBuffer.nbytes` | 差 |
   |---|---|---|---|
   | 641x480 8bit | 461760 | 461280 | +480 |
   | 640x481 8bit | 462080 | 461440 | +640 |
   | 641x481 8bit | 463043 | 461921 | +1122 |
   | 641x481 10bit | 926086 | 923842 | +2244 |

   guard を外して素直に通すと、C-1 と同一の「全フレームが斜めにずれる・例外は出ない」故障になる。

加えて libx264 は 4:2:0 の奇数を 8bit/10bit とも拒否する。**奇数のまま焼く経路は存在しない。**

採る案: パス1の前に検査して止める（`video.py` に `check_render_geometry()`、
`cli.py` の probe 直後で呼ぶ）。`--detect-only` は警告のみで続行。

**`scale` で偶数に丸めるのは不可。** 座標が 0.16% 縮み、同一実行内では誰も検知できない。
丸めるなら `pad`（右下に1px足す。座標系も内容も不変）。実測で往復成功。

`render.py` の guard は外さないこと。

## 字幕付き mkv → mp4 で本当の理由が出ない（監査「その他」）

`cli.py` の `writer.stdin.write(...)` に例外処理が無く、`BrokenPipeError` が
stderr 表示コード（`if writer.returncode not in (0, None): raise RuntimeError(... werr ...)`）
を飛び越す。**stderr は `_drain` が正しく溜めている。溜めたものを表示するコードに
到達していないのが正体。**

隠れている本当の理由:
```
[mp4] Could not find tag for codec subrip in stream #2, codec not currently supported in container
[out#0/mp4] Could not write header (incorrect codec parameters ?): Invalid argument
```
**0バイトの出力ファイルが残る。**

同じ壁がもう1枚: `-map 1:t?` が拾う mkv の添付フォントも mp4 に入らない
（`Could not find tag for codec ttf`）。ASS字幕付き mkv は字幕を直しても添付で死ぬ。

`webapp/jobs.py` は `.mkv`/`.webm` のアップロードを許し、出力を常に `output.mp4` に
固定しているので Web からも踏める。

**パイプが壊れた後に stderr を読めるかは実測済み**: `writer.poll()` は None
（プロセスは生きている）、`werr` は6行すべて揃っていた。`wait()` の後に `join()` してから
読むこと。`communicate()` は `_drain` と二重読みになるので不要。

3段で直す。
1. `writer.stdin.write` を `except OSError` で受けて `break`（Windows では EPIPE でなく
   `OSError [Errno 22]` になることがあるので `BrokenPipeError` では不足）
2. 本番と同じ writer コマンドを `-t 0` で 0 フレーム走らせるプリフライトをパス1の前に置く。
   字幕・添付・奇数解像度・出力先の書き込み権限が全部ヘッダ書き込み時点で判明するので、
   奇数解像度の件と1つの仕掛けで片付く。所要 0.1 秒程度。誤検知なし（実測）。
   **`-t 0` は必須**（付けないと音声を丸ごとコピーする）。
3. 出力が mp4/mov/m4v のとき `-c:s copy` を `-c:s mov_text` に、`-map 1:t?` を外す。
   実測で srt も ass も通り、mkv 出力は従来どおり copy + 添付保持。
   画像字幕（PGS/VobSub）は mov_text に入らないのでプリフライトが捕まえる担当（未検証）。

`review.py` の同型に見える except は変更不要（ブラウザが range 要求を切ったときの
socket 書き込みで、黙るのが正しい）。

## 検証で出た積み残し

- `run_render` が `reader.returncode` を見ていない。パス2のデコードが途中で死ぬと
  短い出力が exit 0 で出る（実測: 39/90 フレーム）。漏れではなく切り詰め
- `review.py` の `/api/mark` が今もフレーム番号をクランプする（webapp 側は 400 にした）
- `review.py` が `estimated_only_ranges()` を使わず `min_len=5` をハードコードしている。
  人手レビュー UI では 1〜4 フレームの推定のみ区間が見えない
- `--limit-frames` で作った短い det.json を読むと検査セッションが素材の一部しか見ず、
  その範囲で「すべて判定済み」に到達できる
- レンダ中に入れた手修正は、その回の完成品に入らないまま「完了」になる
- ジョブ画面に「検出は途中保存」と出ないので「再利用して焼き直す」が黙って数時間のパス1になる
- `tools/make_fake_detections.py` が `complete` を書かず、width/height の既定が 1280x720 のまま
- ガード発動時に再生可能な部分出力が出力パスに残り、webapp の download は status を見ない
- `tests/` が `testsrc2` を使っているが、**`testsrc2` は奇数サイズ指定を黙って偶数に丸める**。
  奇数解像度は原理的にテストできていない。`color=` / `nullsrc=` は奇数を保持する

---

## 最優先: W-1 は実際の画面では直っていない

**監査文書が「いちばん重い」とした W-1 が、それが起きる画面では生きたまま。**

W-6 の舞台であるタイムライン画面を配信しているのは `webapp` ではなく
**`review.py` の `/timeline`**。修正は `webapp/app.py` にだけ入った。

`review.py` の `POST /api/corrections` に残っているもの:
- `data.get("corrections", [])` — キーが無い本文を「空で置き換えろ」と読む
- `s.set_corrections()` の後に `save_progress()` を呼ばない

実サーバで監査文書の3手をそのまま再現できる:
```
A: frame 5-15 を塞ぐ (add 11件)。history=[{frame:10, added:11}] がディスクに保存される
B: タイムラインが frame 40-50 の add 11件を足した一覧を POST -> 200 / 22件
C: 画面を開き直して「ひとつ戻す」-> 22件 -> 11件
   領域があるフレーム(5-15) : [5..15]  （残る）
   領域があるフレーム(40-50): []       ← 直前に足した無関係な11件が全部消えた
```
消えたのは全部 `add`（漏れを塞いだ矩形）で、そのフレームは完全素通しに戻る。
判定は `.progress.json` に残るので「塞いだ」と表示されたまま矩形だけが無い。

本文検証も無い: `{"nothing":1}` を POST するだけで手修正が全消し、200 が返る。

`review.py` にはさらに `/api/mark` のクランプも残っている（`frame=999999` が
最終フレームに記録され、応答は 999999 を返す。`frame=-5` は 0 に記録）。
webapp 側は 400 にしたので、**同じ素材を2つの画面で見ると判定が噛み合わない。**

`review.py` はまとめて片付けること。

## 重大: 新設の「素通しの区間」表示が remove 由来の素通しを拾わない

`n_uncovered_ranges` の元になる `uncovered_ranges` は `stats["_left_open"]`、
つまり **`corr.apply()` を通す前**の値（`cli.py` で `left_open` を取ってから
`regions = corr.apply(...)`）。実測:

```
全フレーム検出 + frame20-29 に remove の手修正
   画面の表示: n_uncovered_ranges=0 / report uncovered=[]
   実測: モザイクあり 50枚 / 素通し 20-29 の10枚
```

「誤検知」判定は add を伴わない bare remove を置くので通常操作で到達する。
W-6 で塞いだのと同じ壊れ方（remove だけ残って素通し）を、この安全表示は検出できない。
**塗り過ぎ側ではなく漏れる側に外れる表示。**

## W-7 の残り

- **壊れた meta.json で二重起動が復活する。** `Library.get()` が壊れた meta を
  握りつぶして pid の無い meta を合成するため、`running_pid()` が「走っていない」と答える。
  実測: 生きた pid 1644 がいる状態で meta を壊す -> 起動した pid=21492
- **2サーバ同時押し（TOCTOU）。** `RunnerRegistry.start()` は `self._lock` の中で
  `running_pid()` を見るが、`r.start()`（Popen と meta への pid 書き込み）はロックの外。
  `--port` は指定可能なので同一ライブラリに2本立てられる。実測で両方起動した
- pid に「起動した時刻」「argv のハッシュ」を添えて照合するのが素直な直し方

## W-6 の残り

- **修正はクライアント限定。** `set_corrections()` は渡された一覧を無検証で受ける。
  古いキャッシュの `timeline.js`、別実装のクライアント、curl から組を割った一覧を
  投げれば素通しは作れる。サーバ側に不変条件が無い
- `review.py` の `_serve_static` は `Cache-Control` も `ETag` も付けないので、
  ブラウザのヒューリスティックキャッシュで旧 `timeline.js` が残りうる
- **`correctionsAfterDrop` / `numOr` の回帰テストがリポジトリに無い。**
  `tests/test_frontend.mjs` は未変更。場当たりの総当たりは実行されたが CI に残っていない

## デッドコード削除の残り

削除は `webapp/static/*.html` だけ。`review.py` が配信する
`automosaic/web/index.html`（117行、「問題なし」「漏れている」「でかすぎる」等の
`<button>` が15個）と `timeline.html`（107行）は手つかず。
**判定画面なので、いちばん悪い場所が残っている。**

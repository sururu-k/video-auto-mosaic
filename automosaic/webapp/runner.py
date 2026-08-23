"""処理の起動と進捗の取り出し。

処理そのものは `python -m automosaic` をサブプロセスで呼ぶ。ライブラリとして
import して同一プロセスで回すと、(1) 3〜4時間まわる推論が Web サーバの
イベントループと同居し、(2) 途中で止めたいときに殺せない、(3) 落ちたら
サーバごと落ちる。別プロセスにしておけば、サーバを再起動しても処理は生き残る。

進捗は既存 CLI の `Progress` クラスが stderr に `\r` 付きで書いているものを
そのまま読む。CLI 側に機械可読な出力を足すと cli.py を変更することになるので、
表示用の文字列を解釈する側に寄せた。行区切りが `\n` ではなく `\r` なので、
read1() で来た分だけ取り、両方の文字で切る。
"""

from __future__ import annotations

import codecs
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

from . import jobs as jobs_mod

# cli.Progress の出力そのもの:
#   "\rパス1 検出 123/4560 ( 26.9%)   14.2 fps  残り 0:12:34   "
RE_PROGRESS = re.compile(
    r"(パス[12])\s+(\S+)\s+(\d+)/(\d+)\s*\(\s*([\d.]+)%\)\s+([\d.]+)\s*fps\s+残り\s+([0-9:]+)"
)
# 総フレーム数が分からないとき:
#   "\rパス1 検出 123 フレーム   14.2 fps   "
RE_PROGRESS_OPEN = re.compile(r"(パス[12])\s+(\S+)\s+(\d+)\s+フレーム\s+([\d.]+)\s*fps")

# 保持するログ行数。3〜4時間ぶんの進捗行を全部持つ意味は無い。
LOG_LIMIT = 400


def child_env() -> dict:
    """サブプロセスの環境。

    Windows のパイプ越しだと Python は既定で cp932 を使い、進捗の日本語が
    化けて正規表現が当たらなくなる。UTF-8 を明示する。
    ffmpeg は winget 版が %LOCALAPPDATA%\\Microsoft\\WinGet\\Links に入り、
    GUI から起動したサーバの PATH には載っていないことがある。見つからない
    ときだけ足す（見つかるなら余計な経路を増やさない）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if shutil.which("ffmpeg") is None:
        links = os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"
        )
        if os.path.isdir(links):
            env["PATH"] = env.get("PATH", "") + os.pathsep + links
    return env


def build_argv(job: jobs_mod.Job, settings: dict, reuse: bool = False) -> list[str]:
    """CLI の引数を組む。

    既定値は cli.py 側に持たせたままにする。ここで書き写すと、CLI の既定を
    変えたときに Web だけ古い値で動く。渡すのは利用者が画面で触った項目だけ。
    """
    argv = [
        sys.executable,
        "-m",
        "automosaic",
        job.source,
        "-o",
        job.output,
        "--detections",
        job.detections,
        "--report",
        job.report,
    ]
    if reuse and os.path.exists(job.detections):
        # 「検出を再利用する」でも、途中保存（complete: false）は再利用できない。
        # 渡すと cli.py がエラーで止める。止まったところから検出を続けて、
        # そのまま焼く（--resume）。数時間ぶんの検出は捨てずに済む。
        #
        # 足りないぶんを直前フレームの領域で埋める逃げ道（--allow-short-detections）
        # は絶対に付けない。後半が素通しの完成品が、正常終了として出てくる経路そのもの。
        state = jobs_mod.detections_complete(job.detections)
        if state is True:
            argv.append("--reuse-detections")
        elif state is False:
            argv.append("--resume")
        # 読めない JSON は再利用も再開もできない。パス1をやり直す
    if os.path.exists(job.corrections):
        argv += ["--corrections", job.corrections]

    def opt(key: str, flag: str, cast=str) -> None:
        v = settings.get(key)
        if v is None or v == "":
            return
        argv.extend([flag, str(cast(v))])

    opt("infer_size", "--infer-size", int)
    opt("conf", "--conf", float)
    opt("classes", "--classes")
    opt("block", "--block", int)
    opt("mode", "--mode")
    opt("margin_scale", "--margin-scale", float)
    opt("margin_cap", "--margin-cap", float)
    opt("crf", "--crf", int)
    opt("provider", "--provider")
    opt("frame_step", "--frame-step", int)
    opt("limit_frames", "--limit-frames", int)
    if settings.get("tta"):
        argv.append("--tta")
    if settings.get("estimate_gaps"):
        argv.append("--estimate-gaps")
    if settings.get("detect_only"):
        argv.append("--detect-only")
    return argv


class JobRunner:
    """1ジョブぶんのサブプロセスと、そこから拾った進捗。

    進捗はメモリにしか無い。落としたくないのは「どこまで終わったか」なので、
    節目（開始・パスの切り替わり・終了）では meta.json に書き戻す。
    毎回の更新を meta に書くと、1秒に2回ディスクへ書き続けることになる。
    """

    def __init__(self, job: jobs_mod.Job, settings: dict, reuse: bool = False) -> None:
        self.job = job
        self.settings = settings
        self.argv = build_argv(job, settings, reuse=reuse)
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.seq = 0
        self.log: deque = deque(maxlen=LOG_LIMIT)
        self.progress: dict = {}
        self.started_at = 0.0
        self.finished_at = 0.0
        self.returncode: int | None = None
        self.canceled = False
        self._threads: list[threading.Thread] = []
        self._logfile = None

    # -- 起動と停止 ------------------------------------------------------
    def start(self) -> None:
        self.started_at = time.time()
        self._logfile = open(self.job.log, "a", encoding="utf-8")
        self._logfile.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        self._logfile.write(" ".join(self.argv) + "\n")
        self._logfile.flush()

        self.proc = subprocess.Popen(
            self.argv,
            cwd=jobs_mod.REPO_ROOT,
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        self.job.update(
            status=jobs_mod.STATUS_RUNNING,
            pid=self.proc.pid,
            argv=self.argv,
            settings=self.settings,
            started_at=self.started_at,
            finished_at=None,
            elapsed_sec=None,
            error=None,
            detached=False,
            progress={},
        )
        for pipe, is_err in ((self.proc.stdout, False), (self.proc.stderr, True)):
            t = threading.Thread(target=self._pump, args=(pipe, is_err), daemon=True)
            t.start()
            self._threads.append(t)
        threading.Thread(target=self._wait, daemon=True).start()

    def cancel(self) -> bool:
        """処理を中断する。中間ファイルは消さない。

        検出まで終わっていれば det.json は残るので、次は --reuse-detections で
        描画だけやり直せる。消してしまうと数時間を捨てることになる。
        """
        if self.proc is None or self.proc.poll() is not None:
            return False
        self.canceled = True
        try:
            self.proc.terminate()
        except OSError:
            return False
        return True

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # -- 出力の読み取り --------------------------------------------------
    def _pump(self, pipe, is_err: bool) -> None:
        """パイプから来た分だけ読み、`\\r` と `\\n` の両方で切る。

        readline() だと `\\r` で終わる進捗行は次の `\\n` が来るまで返らず、
        パス1のあいだ進捗が一切出てこない（数時間、無反応に見える）。

        4096 バイトのチャンク単位で `chunk.decode("utf-8", "replace")` を
        独立に呼ぶと、日本語（3バイト）のようなマルチバイト文字がチャンク
        境界をまたいだときに前半チャンクの末尾・後半チャンクの先頭がそれぞれ
        不正なバイト列になり、両方が置換文字に化ける（issue #30）。
        インクリメンタルデコーダを使い、チャンクをまたぐ文字の内部状態を
        デコーダ自身に持たせることでこれを避ける。
        """
        buf = ""
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = pipe.read1(4096) if hasattr(pipe, "read1") else pipe.read(1)
                if not chunk:
                    break
                buf += dec.decode(chunk)
                parts = re.split(r"[\r\n]", buf)
                buf = parts.pop()
                for line in parts:
                    line = line.rstrip()
                    if line:
                        self._on_line(line, is_err)
        except (ValueError, OSError):
            pass
        finally:
            # ストリーム終端。デコーダ内部に保持されたままの端数バイトを
            # 確定させる（そこで打ち切ると最後の文字が欠ける）。
            buf += dec.decode(b"", final=True)
            if buf.strip():
                self._on_line(buf.strip(), is_err)
            try:
                pipe.close()
            except OSError:
                pass

    def _on_line(self, line: str, is_err: bool) -> None:
        m = RE_PROGRESS.search(line)
        if m:
            self._set_progress(
                m.group(1),
                m.group(2),
                int(m.group(3)),
                int(m.group(4)),
                float(m.group(5)),
                float(m.group(6)),
                m.group(7),
            )
            return
        m = RE_PROGRESS_OPEN.search(line)
        if m:
            self._set_progress(
                m.group(1), m.group(2), int(m.group(3)), 0, 0.0, float(m.group(4)), ""
            )
            return
        self._add_log(line)

    def _add_log(self, line: str) -> None:
        with self.lock:
            self.seq += 1
            self.log.append({"seq": self.seq, "text": line, "t": time.time()})
        if self._logfile:
            try:
                self._logfile.write(line + "\n")
                self._logfile.flush()
            except OSError:
                pass

    def _set_progress(
        self,
        pass_label: str,
        what: str,
        n: int,
        total: int,
        pct: float,
        fps: float,
        eta: str,
    ) -> None:
        key = "pass1" if pass_label.endswith("1") else "pass2"
        with self.lock:
            first = key not in self.progress
            self.seq += 1
            self.progress[key] = {
                "label": f"{pass_label} {what}",
                "n": n,
                "total": total,
                "percent": round(pct, 1) if total else None,
                "fps": fps,
                "eta": eta,
                "seq": self.seq,
                "t": time.time(),
            }
        # パスが切り替わった瞬間だけ meta へ書く。毎回書くと、1秒に2回
        # ディスクへ書き続けることになる
        if first:
            self._flush_meta()

    def _flush_meta(self) -> None:
        with self.lock:
            snap = {k: dict(v) for k, v in self.progress.items()}
        self.job.meta["progress"] = snap
        self.job.save()

    # -- 終了 -------------------------------------------------------------
    def _wait(self) -> None:
        assert self.proc is not None
        rc = self.proc.wait()
        for t in self._threads:
            t.join(timeout=5)
        self.returncode = rc
        self.finished_at = time.time()

        if self.canceled:
            status = jobs_mod.STATUS_CANCELED
            err = "利用者が中断しました"
        elif rc == 0:
            status = jobs_mod.STATUS_DONE
            err = None
        else:
            status = jobs_mod.STATUS_FAILED
            with self.lock:
                tail = [e["text"] for e in list(self.log)[-8:]]
            err = f"終了コード {rc}\n" + "\n".join(tail)

        with self.lock:
            snap = {k: dict(v) for k, v in self.progress.items()}
            self.seq += 1
        self.job.meta.update(
            status=status,
            progress=snap,
            returncode=rc,
            finished_at=self.finished_at,
            elapsed_sec=round(self.finished_at - self.started_at, 1),
            error=err,
            pid=None,
            n_corrections=jobs_mod.count_corrections(self.job),
        )
        self._merge_report()
        self.job.save()
        if self._logfile:
            try:
                self._logfile.close()
            except OSError:
                pass
            self._logfile = None

    def _merge_report(self) -> None:
        """CLI が書いたレポートから統計だけ meta に取り込む。

        レポート本体は report.json に残す。meta に入れるのは一覧と
        案件間の比較に使う数字だけ。あとで「どの設定で何件漏れたか」を
        集計できる形にしておく。
        """
        import json

        try:
            with open(self.job.report, encoding="utf-8") as f:
                rep = json.load(f)
        except (OSError, ValueError):
            return
        self.job.meta["stats"] = rep.get("stats") or {}
        self.job.meta["n_uncovered_ranges"] = len(rep.get("uncovered_ranges") or [])
        self.job.meta["n_estimated_only_ranges"] = len(
            rep.get("estimated_only_ranges") or []
        )
        self.job.meta["n_review_frames"] = len(rep.get("review_frames") or [])

    # -- 送出用 -----------------------------------------------------------
    def snapshot(self, since: int = 0) -> dict:
        """進捗の現状。since より新しいログ行だけ足す。

        進捗そのものは積み上げでなく現在値なので、途中で接続が切れても
        繋ぎ直した時点の値を1回送れば追いつく。ログだけ取りこぼしうるので
        連番で欠けを埋められるようにしてある。
        """
        with self.lock:
            lines = [e for e in self.log if e["seq"] > since]
            snap = {k: dict(v) for k, v in self.progress.items()}
            seq = self.seq
        return {
            "job": self.job.id,
            "status": self.job.status,
            "alive": self.alive(),
            "progress": snap,
            "log": lines,
            "seq": seq,
            "returncode": self.returncode,
            "elapsed_sec": round(
                (self.finished_at or time.time()) - self.started_at, 1
            )
            if self.started_at
            else None,
        }


class RunnerRegistry:
    """走っているジョブの一覧。1ジョブ1本まで。"""

    def __init__(self) -> None:
        self._runners: dict[str, JobRunner] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> JobRunner | None:
        with self._lock:
            return self._runners.get(job_id)

    def start(self, job: jobs_mod.Job, settings: dict, reuse: bool = False) -> JobRunner:
        """1本だけ起動する。既に走っていれば RuntimeError。

        弾く根拠を2つ持つ。レジストリはこのサーバのメモリにしか無いので、
        サーバを再起動するとサブプロセスだけが生き残った状態（detached）に
        なり、レジストリは空になる。そこを見ていないと、同じ output.mp4 へ
        2本が同時に書き込む。meta.json の pid でも必ず確かめる。
        """
        with self._lock:
            cur = self._runners.get(job.id)
            # proc が None なのは「登録したが Popen する前」の一瞬。そこを
            # 空きと見なすと、続けて2回押されたときに2本走る
            if cur is not None and (cur.proc is None or cur.alive()):
                raise RuntimeError("このジョブは既に実行中です")
            pid = jobs_mod.running_pid(job)
            if pid is not None:
                raise RuntimeError(
                    f"このジョブは別のプロセス (pid {pid}) が実行中です。"
                    "終わるまで待つか、そのプロセスを終了させてください"
                )
            r = JobRunner(job, settings, reuse=reuse)
            self._runners[job.id] = r
        try:
            r.start()
        except Exception:
            # 起動に失敗したものを残すと、上の proc is None の判定に引っかかって
            # そのジョブが二度と起動できなくなる
            with self._lock:
                if self._runners.get(job.id) is r:
                    del self._runners[job.id]
            raise
        return r

    def cancel(self, job_id: str) -> bool:
        r = self.get(job_id)
        return bool(r and r.cancel())

    def active(self) -> list[str]:
        with self._lock:
            return [k for k, v in self._runners.items() if v.alive()]

    def shutdown(self) -> None:
        """サーバ終了時。走っているものは殺さない。

        3〜4時間の処理を、サーバの再起動ごとに巻き戻すわけにいかない。
        プロセスは走らせたまま、meta.json の pid から再起動後に生存を確認する。
        """
        for r in list(self._runners.values()):
            if r.alive():
                r.job.update(detached=True)

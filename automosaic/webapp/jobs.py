"""ジョブの受け皿。`data/library/<ジョブID>/` を1件の作業単位として扱う。

1件の中に素材・検出結果・手修正・完成品・学習データを全部入れておく。
案件をまたいで共有する状態を作らないので、ディレクトリごと移動しても
消しても、他の案件に影響しない。

meta.json は「サーバが落ちても状態を復元できる」ことを目的に置いている。
実行中の進捗はメモリにしか無いが、どこまで進んだか・何を実行したかは
meta.json に書き戻す。サーバを再起動したときは、そこから読み直して
続きの操作（再開・再描画・検査）ができる状態に戻す。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass

# 既定のライブラリ位置。リポジトリ直下の data/library/。
# data/myvideo5 や data/bench3 のような既存の作業ディレクトリとは分ける。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_LIBRARY = os.path.join(REPO_ROOT, "data", "library")

# ジョブID は日時ベース。一覧が自然に時系列で並び、フォルダ名から
# いつの案件か読める。同じ秒に2件作られても衝突しないよう乱数を足す。
JOB_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")

STATUS_NEW = "new"              # 素材だけある
STATUS_QUEUED = "queued"        # 起動を受け付けた
STATUS_RUNNING = "running"      # サブプロセス実行中
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"
STATUS_INTERRUPTED = "interrupted"  # サーバか OS 側で落ちた

# 受け付ける拡張子。ffmpeg が読めるものに限る必要はないが、
# 素材以外を置き場に流し込まれる筋は塞いでおく。
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".ts", ".wmv", ".flv"}


def new_job_id(library: str) -> str:
    """衝突しないジョブID を作る。"""
    for _ in range(100):
        jid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        if not os.path.exists(os.path.join(library, jid)):
            return jid
    raise RuntimeError("ジョブID を確保できません")


def safe_ext(filename: str) -> str:
    """アップロード名から拡張子だけ取り出す。

    ファイル名そのものは保存名に使わない。パス区切りや Unicode の細工で
    ライブラリの外へ書かせる経路を作らないため、保存名は source<ext> に固定し、
    元の名前は meta.json の中に文字列として持つだけにする。
    """
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in ALLOWED_EXT else ".mp4"


@dataclass
class Job:
    """1件の案件。ディレクトリと meta.json の薄い包み。"""

    id: str
    dir: str
    meta: dict

    # -- パス -----------------------------------------------------------
    def path(self, *parts: str) -> str:
        return os.path.join(self.dir, *parts)

    @property
    def source(self) -> str:
        return self.path(self.meta.get("source") or "source.mp4")

    @property
    def output(self) -> str:
        return self.path("output.mp4")

    @property
    def detections(self) -> str:
        return self.path("det.json")

    @property
    def corrections(self) -> str:
        return self.path("corrections.json")

    @property
    def annotations(self) -> str:
        return self.path("annotations.json")

    @property
    def report(self) -> str:
        return self.path("report.json")

    @property
    def dataset(self) -> str:
        return self.path("dataset")

    @property
    def log(self) -> str:
        return self.path("run.log")

    # -- 状態 -----------------------------------------------------------
    @property
    def status(self) -> str:
        return self.meta.get("status", STATUS_NEW)

    def save(self) -> None:
        """meta.json を書く。書き換え中に落ちても壊れないよう置き換えで書く。"""
        self.meta["updated_at"] = time.time()
        tmp = self.path("meta.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.meta, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path("meta.json"))

    def update(self, **kw) -> "Job":
        self.meta.update(kw)
        self.save()
        return self

    def summary(self) -> dict:
        """一覧に出す分だけ。meta 全体を返すと設定やログまで載って重い。"""
        m = self.meta
        return {
            "id": self.id,
            "name": m.get("name") or self.id,
            "created_at": m.get("created_at"),
            "status": self.status,
            "size_bytes": m.get("size_bytes", 0),
            "width": m.get("width"),
            "height": m.get("height"),
            "fps": m.get("fps"),
            "n_frames": m.get("n_frames"),
            "duration_sec": m.get("duration_sec"),
            "has_source": os.path.exists(self.source),
            "has_detections": os.path.exists(self.detections),
            "has_output": os.path.exists(self.output),
            "has_dataset": os.path.isdir(self.dataset),
            "n_corrections": m.get("n_corrections", 0),
            "elapsed_sec": m.get("elapsed_sec"),
            "progress": m.get("progress") or {},
            "error": m.get("error"),
        }

    def detail(self) -> dict:
        d = self.summary()
        d["settings"] = self.meta.get("settings") or {}
        d["stats"] = self.meta.get("stats") or {}
        d["argv"] = self.meta.get("argv") or []
        d["output_size_bytes"] = (
            os.path.getsize(self.output) if os.path.exists(self.output) else 0
        )
        return d


class Library:
    """ジョブ置き場。ディレクトリを走査するだけの薄い層。

    一覧をメモリにキャッシュしない。ジョブは人が作る速さでしか増えず、
    ディレクトリ走査で足りる。キャッシュを持つと、サーバを再起動せずに
    フォルダを直接いじったときに実体と食い違う。
    """

    def __init__(self, root: str | None = None) -> None:
        self.root = os.path.abspath(root or DEFAULT_LIBRARY)
        os.makedirs(self.root, exist_ok=True)

    # -- 取得 -----------------------------------------------------------
    def job_dir(self, job_id: str) -> str:
        if not JOB_ID_RE.match(job_id or ""):
            raise KeyError(job_id)
        return os.path.join(self.root, job_id)

    def get(self, job_id: str) -> Job:
        d = self.job_dir(job_id)
        meta_path = os.path.join(d, "meta.json")
        if not os.path.isfile(meta_path):
            raise KeyError(job_id)
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            # meta が壊れていても素材は残っている。読めないことを状態として出す
            meta = {"id": job_id, "status": STATUS_FAILED, "error": "meta.json を読めません"}
        return Job(id=job_id, dir=d, meta=meta)

    def list(self) -> list[Job]:
        out: list[Job] = []
        for name in os.listdir(self.root):
            if not JOB_ID_RE.match(name):
                continue
            try:
                out.append(self.get(name))
            except KeyError:
                continue
        # 新しいものを上に。ジョブID が日時なので文字列の降順でよい
        out.sort(key=lambda j: j.id, reverse=True)
        return out

    # -- 作成 -----------------------------------------------------------
    def create(self, name: str) -> Job:
        """空のジョブを作る。素材の書き込みは呼び出し側が行う。

        アップロードは数 GB になる。ここでファイル本体を受け取る形にすると
        メモリに載せる実装を誘発するので、置き場だけ作って返す。
        """
        jid = new_job_id(self.root)
        d = os.path.join(self.root, jid)
        os.makedirs(d, exist_ok=False)
        job = Job(
            id=jid,
            dir=d,
            meta={
                "id": jid,
                "name": name or jid,
                "source": "source" + safe_ext(name),
                "created_at": time.time(),
                "status": STATUS_NEW,
                "size_bytes": 0,
                "settings": {},
                "progress": {},
            },
        )
        job.save()
        return job

    def delete(self, job_id: str) -> None:
        """ジョブを丸ごと消す。素材ごと消えるので呼ぶ側で確認を取ること。"""
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)


def probe_source(job: Job) -> dict:
    """素材の解像度・fps・フレーム数を meta に入れる。

    ffprobe ではなく cv2 で読む。review.probe_with_cv2 と同じ理由で、
    ここは「アップロード直後に一覧へ出す情報」でしかなく、ffmpeg が
    無い環境でもアップロードだけは通ってほしい。
    """
    from ..review import probe_with_cv2

    try:
        w, h, fps, n = probe_with_cv2(job.source)
    except Exception as e:  # noqa: BLE001
        job.update(probe_error=str(e))
        return job.meta
    job.meta.update(
        width=w,
        height=h,
        fps=round(float(fps), 3),
        n_frames=int(n),
        duration_sec=round(n / fps, 2) if fps else None,
    )
    job.meta.pop("probe_error", None)
    job.save()
    return job.meta


def count_corrections(job: Job) -> int:
    """手修正の件数。一覧の「どれだけ直したか」に出す。"""
    try:
        with open(job.corrections, encoding="utf-8") as f:
            return len(json.load(f).get("corrections", []))
    except (OSError, ValueError):
        return 0


def pid_alive(pid: int | None) -> bool:
    """プロセスが生きているか。サーバ再起動後の状態復元に使う。

    Windows には os.kill(pid, 0) が無いので OpenProcess 相当を使う。
    ここで嘘をつくと「終わっているのに実行中と表示され続ける」ジョブが
    残り、再実行できなくなる。
    """
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def settle(job: Job) -> bool:
    """実行中表示のまま止まっているジョブを現実に合わせる。直したら True。

    サーバが落ちるとサブプロセスへの参照は失われる。プロセスが消えている
    のに meta が running のままだと、そのジョブは二度と起動できない
    見た目になる。完成品が残っていれば「焼き上がってから落ちた」なので
    完了として扱い、無ければ中断として再開できると分かるようにする。
    """
    if job.status not in (STATUS_RUNNING, STATUS_QUEUED):
        return False
    if pid_alive(job.meta.get("pid")):
        # 3〜4時間かかる処理を巻き戻さない。走っているものはそのままにする
        if not job.meta.get("detached"):
            job.update(detached=True)
        return False
    done = os.path.exists(job.output) and os.path.getsize(job.output) > 0
    job.update(
        status=STATUS_DONE if done else STATUS_INTERRUPTED,
        pid=None,
        detached=False,
        error=None if done else "サーバの停止で処理が中断されました",
    )
    return True


def reconcile(library: Library) -> list[str]:
    """起動時に全ジョブを突き合わせる。直したジョブID を返す。"""
    return [job.id for job in library.list() if settle(job)]

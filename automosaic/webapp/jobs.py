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
    """衝突しないジョブID を作り、その場でディレクトリを作って確保する。

    「存在しなければ返す」だけだと、確保していない ID を返すことになる。
    見てから使うまでの間に別の要求が同じ ID を引き当てられるので、実際の
    一意性は乱数4桁（16bit）だけに乗る。同じ秒に 50 件作れば 2% ほどで
    衝突し、あとから来たほうが先の案件のディレクトリに素材を上書きする。

    作る側で排他（exist_ok=False）を取り、作れたものだけを返す。
    戻り値の ID のディレクトリは、この関数を抜けた時点で既に存在する。
    """
    os.makedirs(library, exist_ok=True)
    for _ in range(100):
        jid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        try:
            os.mkdir(os.path.join(library, jid))
        except FileExistsError:
            continue
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
    def proxy(self) -> str:
        """確認用プロキシ動画（issue #18）。output.mp4 からのみ作る。"""
        return self.path("proxy.mp4")

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
        # 素通しの区間数は、完成品を受け取る前に見えていないと意味がない。
        # 「完了・ダウンロード」しか語らない画面だと、モザイクが1つも
        # 乗っていない区間があることに誰も気づけない。
        # 数字は _merge_report が meta に入れてある（レポートは読み直さない）
        d["n_uncovered_ranges"] = self.meta.get("n_uncovered_ranges")
        d["n_estimated_only_ranges"] = self.meta.get("n_estimated_only_ranges")
        # プロキシの状態。「まだ無い」（None）と「作れなかった」（failed）を
        # 画面が区別できるよう、has_proxy だけでなく status も別に出す。
        p = self.meta.get("proxy") or {}
        d["proxy_status"] = p.get("status")
        d["proxy_error"] = p.get("error")
        d["has_proxy"] = os.path.exists(self.proxy)
        d["proxy_size_bytes"] = (
            os.path.getsize(self.proxy) if os.path.exists(self.proxy) else 0
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
        # new_job_id がディレクトリごと確保して返す。ここで作り直さない
        jid = new_job_id(self.root)
        d = os.path.join(self.root, jid)
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
        try:
            job.save()
        except OSError:
            # meta.json を書けなかったディレクトリは、一覧にも出ず消せもしない
            # 孤児になる（list は meta の無いものを読み飛ばし、get は 404）。
            # 確保しただけのものは、ここで片付けてから投げる
            shutil.rmtree(d, ignore_errors=True)
            raise
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


def detections_complete(path: str) -> bool | None:
    """検出結果 JSON が最後まで終わっているか。読めなければ None。

    `--checkpoint-every` が書く途中保存には `complete: false` が入っている。
    これを `--reuse-detections` に渡すと、cli.py がエラーで止める（止めないと
    後半が丸ごと未検出のまま焼かれ、素通しの完成品が正常終了として出てくる）。
    Web から押した操作がその場で失敗しないよう、渡す前にこちらで見分ける。

    `complete` が無い古い形式は、cli.py と同じく完了扱いにする。
    """
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d.get("complete", True) is not False


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


def process_creation_ticks(pid: int | None) -> int | None:
    """プロセスの起動時刻を、比較にしか使わない整数値で返す。取れなければ None。

    OS が pid を再利用すると、同じ pid でも実体は別プロセスになる
    （issue #44）。pid が生きているかどうかだけでは、それが本当に
    このジョブの処理プロセスなのか、たまたま同じ番号を割り当てられた
    無関係な生きたプロセスなのかを区別できない。起動時刻は再利用された
    プロセスとはまず一致しないので、記録しておいて後で照合する材料にする。
    """
    if not pid:
        return None
    if os.name == "nt":
        import ctypes

        class _FILETIME(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            creation = _FILETIME()
            exit_t = _FILETIME()
            kernel_t = _FILETIME()
            user_t = _FILETIME()
            ok = k32.GetProcessTimes(
                h,
                ctypes.byref(creation),
                ctypes.byref(exit_t),
                ctypes.byref(kernel_t),
                ctypes.byref(user_t),
            )
            if not ok:
                return None
            return (creation.high << 32) | creation.low
        finally:
            k32.CloseHandle(h)
    # POSIX: /proc/<pid>/stat の22番目のフィールド（システム起動からの
    # jiffies 単位の起動時刻）。/proc の無い環境（mac 等）では取れない。
    # 取れない場合は照合を諦め、呼び出し側は pid の生死だけで判断する
    # 従来の挙動にとどまる（新たに拒否を増やす方向には倒さない）。
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="ascii") as f:
            data = f.read()
        # comm フィールド（カッコの中）に空白や閉じ括弧が入りうるので、
        # 最後の ")" より後ろから数える
        after = data.rsplit(")", 1)[1].split()
        return int(after[19])  # state から数えて22番目 = starttime
    except (OSError, ValueError, IndexError):
        return None


def running_pid(job: Job) -> int | None:
    """このジョブを処理しているプロセスの pid。走っていなければ None。

    走っているかどうかの根拠は meta.json の pid と、その pid が実在するか
    どうか。サーバのメモリ上のレジストリはサーバを再起動すると空になるが、
    サブプロセスは生き残る（RunnerRegistry.shutdown を見よ）。レジストリ
    だけを見て二重起動を弾いていると、再起動をまたいだ瞬間に同じ
    output.mp4 へ2本が書き込む状態を作れてしまう。

    死んだプロセスの pid には pid_alive が False を返すので、落ちたサーバの
    残骸でジョブが永久に起動できなくなることはない。

    それだけでは足りない場合がある（issue #44）。元のプロセスが終了した
    あとに OS がその pid を無関係な別プロセスへ再利用すると、pid_alive は
    「生きている」と答え続け、そのジョブは永久に起動不能・キャンセル不能に
    なる。meta.json に起動時刻（pid_started_ticks、JobRunner.start が記録）
    があれば、生死だけでなく起動時刻も一致することまで確認する。一致しない
    ものは「別プロセスに入れ替わっている」とみなして走っていない扱いにする。
    起動時刻が記録されていない古い meta（この修正より前のジョブ）は、
    従来どおり生死だけで判断する。
    """
    pid = job.meta.get("pid")
    if not pid:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if not pid_alive(pid):
        return None
    recorded = job.meta.get("pid_started_ticks")
    if recorded is not None:
        current = process_creation_ticks(pid)
        if current is None or int(current) != int(recorded):
            # 生きてはいるが、記録した起動時刻と一致しない = 別プロセスに
            # 入れ替わっている（pid が再利用された）。走っていない扱いにする
            return None
    return pid


def settle(job: Job) -> bool:
    """実行中表示のまま止まっているジョブを現実に合わせる。直したら True。

    サーバが落ちるとサブプロセスへの参照は失われる。プロセスが消えている
    のに meta が running のままだと、そのジョブは二度と起動できない
    見た目になる。完成品が残っていれば「焼き上がってから落ちた」なので
    完了として扱い、無ければ中断として再開できると分かるようにする。

    pid の生死だけでなく running_pid() で起動時刻の一致まで見る（issue #44）。
    そうしないと、pid が無関係な別プロセスに再利用された場合にここが
    「まだ実行中」と判定し続け、ジョブ一覧やジョブ画面を開いても永久に
    running のまま固まる（UI からの回復手段が無くなる）。
    """
    if job.status not in (STATUS_RUNNING, STATUS_QUEUED):
        return False
    if running_pid(job) is not None:
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

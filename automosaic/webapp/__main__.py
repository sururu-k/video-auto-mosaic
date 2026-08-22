"""起動口。`python -m automosaic.webapp` で立てる。

既定は 127.0.0.1。端末から見るときだけ --host 0.0.0.0 を明示させる。
LAN に出す判断を既定にしない。トークンは毎回ランダムで、起動時に URL ごと
表示する（QR も review 側の実装をそのまま使う）。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

from .. import review
from . import jobs as jobs_mod


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m automosaic.webapp",
        description="アップロードから完成品の受け取りまでを扱う Web アプリ",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--library",
        default=jobs_mod.DEFAULT_LIBRARY,
        help="ジョブ置き場",
    )
    p.add_argument("--host", default="127.0.0.1", help="待ち受けアドレス。端末から見るなら 0.0.0.0")
    p.add_argument("--port", type=int, default=8770, help="待ち受けポート")
    p.add_argument("--token", help="アクセストークンを固定する（既定は毎回ランダム）")
    p.add_argument(
        "--no-token",
        action="store_true",
        help="トークン検証を切る。127.0.0.1 でのみ使うこと",
    )
    p.add_argument("--no-qr", action="store_true", help="QR コードを出さない")
    p.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    return p


def banner(host: str, port: int, token: str, library: str, no_qr: bool) -> str:
    q = f"?t={token}" if token else ""
    local = f"http://127.0.0.1:{port}/{q}"
    print(f"ライブラリ  {library}")
    n = len([d for d in os.listdir(library) if jobs_mod.JOB_ID_RE.match(d)])
    print(f"            ジョブ {n} 件")
    exposed = host not in ("127.0.0.1", "localhost", "::1")
    if exposed:
        print("\n注意: LAN に公開しています。同じネットワークの端末から見えます")
        if not token:
            print("注意: トークン検証が切れています。誰でも開けます")
    print("\n手元の PC:")
    print(f"  {local}")
    urls = [local]
    if exposed:
        for a in review.lan_addresses():
            u = f"http://{a}:{port}/{q}"
            print(f"  {u}")
            urls.append(u)
    qr_url = urls[1] if len(urls) > 1 else local
    if not no_qr:
        print(f"\n{qr_url}")
        review.print_qr(qr_url)
    print("\nCtrl-C で終了")
    return local


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn がありません。"
            ".venv\\Scripts\\python.exe -m pip install fastapi uvicorn[standard] python-multipart",
            file=sys.stderr,
        )
        return 1

    os.makedirs(args.library, exist_ok=True)
    token = "" if args.no_token else (args.token or review.make_token())

    from .app import create_app

    app = create_app(library_dir=args.library, token=token, require_token=not args.no_token)

    changed = jobs_mod.reconcile(jobs_mod.Library(args.library))
    if changed:
        print(f"中断されたままのジョブを {len(changed)} 件、中断済みに直しました")

    url = banner(args.host, args.port, token, os.path.abspath(args.library), args.no_qr)
    sys.stdout.flush()
    if not args.no_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except OSError as e:
        print(f"{args.host}:{args.port} を開けません: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

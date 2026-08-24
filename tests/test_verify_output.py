"""issue #13: verify_output.py が出荷ゲートとして機能していない件の回帰テスト。

確認していること:
- source モードで本番既定と同じ重みを使ったら、必ず警告が出ること
  （警告が出ない組み合わせに黙って倒れていないか）
- `PROD_MODEL_BASENAME` / `PROD_CONF` が `automosaic/cli.py` の実際の既定と
  ズレていないこと。ズレると警告が的外れになる
- `--min-outside` の既定が 0.5 に戻っていないこと
  （検出矩形の49%がモザイクの外でも報告しない値だった）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automosaic.cli as cli  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import verify_output as vo  # noqa: E402


def test_warning_fires_for_source_mode_with_prod_model():
    """source + 本番既定の重み、は最悪の組み合わせ（循環）。黙って通してはいけない。"""
    msg = vo.structural_warning("source", "C:/whatever/weights/640m.onnx")
    assert msg is not None, "本番既定と同じ重みなのに警告が出ていない"
    assert "循環" in msg or "検証" in msg
    print("  source + 640m.onnx で警告が出る OK")


def test_warning_matches_basename_not_full_path():
    """パスの区切りや大文字小文字ではなく、ファイル名だけで判定していること。"""
    msg = vo.structural_warning("source", os.path.join("weights", "640m.onnx"))
    assert msg is not None
    print("  相対パスでも basename で判定される OK")


def test_no_warning_for_output_mode():
    """output モードは別の理由で使い物にならないとdocstringに明記済み。二重警告しない。"""
    msg = vo.structural_warning("output", "weights/640m.onnx")
    assert msg is None
    print("  output モードでは警告なし OK")


def test_no_warning_for_different_model():
    """本番既定と別の重みを使っているなら、この警告の対象ではない。"""
    msg = vo.structural_warning("source", "weights/some_other_model.onnx")
    assert msg is None, "別モデルなのに警告が出ている（誤検知）"
    print("  別モデルなら警告なし OK")


def test_prod_constants_match_cli_defaults():
    """警告文が引用する本番既定の値が、実際の automosaic/cli.py の既定とズレていないこと。

    ズレたまま気づかないと、この警告自体が誤った前提で出続けることになる。
    """
    parser = cli.build_parser()
    defaults = vars(parser.parse_args([]))
    assert os.path.basename(defaults["model"]) == vo.PROD_MODEL_BASENAME, (
        f"cli.py の既定モデルが {defaults['model']} なのに "
        f"vo.PROD_MODEL_BASENAME は {vo.PROD_MODEL_BASENAME}"
    )
    assert defaults["conf"] == vo.PROD_CONF, (
        f"cli.py の既定 conf が {defaults['conf']} なのに vo.PROD_CONF は {vo.PROD_CONF}"
    )
    print("  PROD_MODEL_BASENAME / PROD_CONF が cli.py の実際の既定と一致 OK")


def test_min_outside_default_is_not_the_broken_value():
    """--min-outside の既定が 0.5 (検出矩形の49%が外でも黙る) に戻っていないこと。"""
    args = vo.build_parser().parse_args(["dummy.mp4", "--detections", "dummy.json"])
    assert args.min_outside != 0.5, "--min-outside の既定が 0.5 に戻っている"
    assert args.min_outside <= 0.1, f"--min-outside の既定 {args.min_outside} が緩すぎる"
    print(f"  --min-outside の既定 {args.min_outside} OK")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"{len(tests)} 件のテストを実行\n")
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'すべて通過' if failed == 0 else f'{failed} 件失敗'}")
    sys.exit(1 if failed else 0)

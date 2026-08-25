"""mosaic_offset 機能の検証。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.temporal import apply_offset, TemporalConfig  # noqa: E402


def test_apply_offset_basic():
    """apply_offset が基本的に正しく動作すること。"""
    box = (100.0, 100.0, 50.0, 50.0)

    # オフセットなし
    result = apply_offset(box, (0.0, 0.0), 1920, 1080)
    assert result == box, "オフセット(0,0)で箱が変わってはいけない"

    # 正のオフセット
    result = apply_offset(box, (50.0, 100.0), 1920, 1080)
    assert result == (150.0, 200.0, 50.0, 50.0), f"正オフセットが違う: {result}"

    print("  apply_offset 基本動作 OK")


def test_mosaic_offset_default():
    """TemporalConfig の mosaic_offset デフォルト値は (0, 0)。"""
    cfg = TemporalConfig()
    assert cfg.mosaic_offset == (0.0, 0.0), f"デフォルトが違う: {cfg.mosaic_offset}"
    print("  mosaic_offset デフォルト OK")


def test_mosaic_offset_clamp():
    """apply_offset が画面外をクランプすること。"""
    box = (100.0, 100.0, 50.0, 50.0)

    # 画面外に出るオフセット
    result = apply_offset(box, (2000.0, 2000.0), 1920, 1080)
    # x は min(100 + 2000, 1920 - 1) = 1919
    # y は min(100 + 2000, 1080 - 1) = 1079
    assert result[0] == 1919, f"x クランプが違う: {result[0]}"
    assert result[1] == 1079, f"y クランプが違う: {result[1]}"

    print("  mosaic_offset クランプ OK")


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

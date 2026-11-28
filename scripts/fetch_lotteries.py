#!/usr/bin/env python3
"""抽選情報の日次取得（daily-update ワークフロー用）。

実装は watch_lotteries.py に一本化されている（短間隔の発表監視と
日次バッチで同じ検出・重複排除・状態管理を使うため）。
このスクリプトは後方互換の入口として watcher をそのまま実行する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch_lotteries  # noqa: E402


def main() -> int:
    return watch_lotteries.main()


if __name__ == "__main__":
    sys.exit(main())

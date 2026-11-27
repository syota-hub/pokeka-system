#!/usr/bin/env python3
"""価格の日次取得（v1）。

取得先:
  - トレカプロ相場 (tcgpro.co.jp / soubapro.com): シングル買取価格の集計
  - 買取ホムラ (kaitori-homura.com): 未開封BOX買取表（シュリンク有無別）、PSA10買取表

出力: data/prices.json
  { "updated_at": ISO, "items": { master_id: {buyback, flea, sale, psa:{10..6, low}} } }

注意: 各サイトのHTML構造は変わりうる。パーサは構造変更に弱い前提で、
取得できなかった品目は前回の値を保持する（消さない）。
サイト側の負荷にならないよう、リクエスト間隔を空け、User-Agentを明示する。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICES_PATH = ROOT / "data" / "prices.json"
MASTER_PATH = ROOT / "data" / "master.json"
UA = {"User-Agent": "pokeka-system/1.0 (personal asset tracker)"}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def main() -> int:
    prices = load_json(PRICES_PATH, {"updated_at": None, "items": {}})
    master = load_json(MASTER_PATH, {"singles": [], "boxes": [], "sets": []})

    # TODO(v1実装): トレカプロ相場のパース
    #   - シングル: master.singles の name/number で照合し buyback を更新
    # TODO(v1実装): 買取ホムラのパース
    #   - BOX買取表: master.boxes の name で照合（シュリンク有り価格を buyback に）
    #   - PSA10買取表: master.singles に照合し psa["10"] を更新
    # 取得できない日は既存値を保持するのが原則。ここでは枠だけ用意し、
    # パーサはサイト構造の実地確認後に有効化する。
    print(f"singles={len(master.get('singles', []))} boxes={len(master.get('boxes', []))}")
    print("parsers not yet enabled; prices.json left unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())

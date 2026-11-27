#!/usr/bin/env python3
"""新商品発表の監視（短間隔ワークフロー用）。

ポケカ公式の商品・お知らせページを軽く確認し、未登録の新商品があれば
data/products.json に追加する。新商品に紐づく抽選は fetch_lotteries 側で
同じ速さで追う前提（このスクリプトは products のみ担当）。

GitHub Actions の定期実行には数分〜十数分の遅延がありうるため、
「発表と同時」は 15〜30 分以内の反映が現実の粒度。
"""
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRODUCTS_PATH = ROOT / "data" / "products.json"
UA = {"User-Agent": "pokeka-system/1.0 (personal new-product watch)"}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def main() -> int:
    data = {"updated_at": None, "items": []}
    if PRODUCTS_PATH.exists():
        data = json.loads(PRODUCTS_PATH.read_text(encoding="utf-8"))
    known = {it["id"] for it in data["items"]}
    # TODO(v1実装): 公式商品ページのパース（商品名／種類／発売日／定価／URL）
    print(f"existing products={len(known)}; parser not yet enabled")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""抽選情報の日次取得（v1）。

取得先:
  - ポケモンセンターオンライン サポート/お知らせ（定型フォーマット:
    応募受付期間／抽選結果発表日／注文期間 の表）
  - ポケカ抽選図鑑 (pokeca-navi.jp): 受付中抽選の集約（地域データあり）
  - スニーカーダンク抽選まとめ記事

出力: data/lotteries.json（id で重複排除し、期間が過ぎたものは残すが
フロント側で締切順・受付中優先に並べる）
"""
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOTTERIES_PATH = ROOT / "data" / "lotteries.json"
UA = {"User-Agent": "pokeka-system/1.0 (personal lottery board)"}

AREAS = [
    "全国", "大手通販サイト", "大手カードショップ", "札幌",
    "宮城（山形・福島含む）", "埼玉", "千葉", "東京", "神奈川",
    "愛知（三重・岐阜含む）", "大阪（京都・兵庫・奈良・和歌山含む）",
    "広島", "福岡（佐賀・大分・熊本含む）",
]


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def main() -> int:
    data = {"updated_at": None, "items": []}
    if LOTTERIES_PATH.exists():
        data = json.loads(LOTTERIES_PATH.read_text(encoding="utf-8"))
    known = {it["id"] for it in data["items"]}
    # TODO(v1実装): ポケセン告知ページのパース（定型表→期間・発表日を抽出）
    # TODO(v1実装): ポケカ抽選図鑑のパース（地域は AREAS のいずれかに正規化）
    print(f"existing lotteries={len(known)}; parsers not yet enabled")
    return 0


if __name__ == "__main__":
    sys.exit(main())

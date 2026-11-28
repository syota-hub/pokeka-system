#!/usr/bin/env python3
"""抽選告知の自動監視（短間隔ワークフロー用）。

公開ページを定期ポーリングし、新しいポケカ抽選販売の告知を検出して
data/lotteries.json に自動登録する。確信を持って抽出できない告知は
data/lotteries.json には入れず、data/watch-candidates.json に
「何が不足しているか」と一緒にステージングする（精度優先）。

ソースと取得可否（2026-11-28 に実測）:
  - ポケカ抽選図鑑 pokeca-navi.jp/lotteries/ : 取得可。サーバー描画で
    店舗/商品/エリア/締切/応募URL/申込要点が構造化されている（主力の集約源）
  - ポケモンセンターオンライン お知らせ詳細 (news/?id=YYYYMMDD):
    詳細ページは取得可。ただし期間詳細（時刻・結果発表・注文期限）は
    画像で掲載されるため、テキストから完全抽出できない場合は候補行き。
    一覧ページはこのネットワークから 500 のため、新着 id の発見は
    (a) 日付プローブ (b) navi・公式お知らせからのリンク で行う
  - ポケモンカード公式お知らせ pokemon-card.com/info/ : 取得可。
    ポケセン告知への外部リンクの発見源として使う
  - ゲオ/ヨドバシ/ビックカメラ/楽天ブックス/あみあみ: 直接取得は
    ボット遮断（403/タイムアウト）または告知一覧ページが存在しないため
    パース対象外。到達性だけを記録し、告知自体は navi 経由で拾う

状態管理: data/watch-state.json に既見キーを保持し、増分実行する。
初回は現在の lotteries.json の登録済み告知と突き合わせて既知扱いにし、
再登録しない。重複判定は「店舗グループ + 商品トークンの重なり +
応募期間（締切日）の一致」。

Python 標準ライブラリのみ。
"""
import hashlib
import html
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOTTERIES_PATH = DATA / "lotteries.json"
CANDIDATES_PATH = DATA / "watch-candidates.json"
STATE_PATH = DATA / "watch-state.json"

JST = timezone(timedelta(hours=9))
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml",
}

AREAS = [
    "全国", "大手通販サイト", "大手カードショップ", "札幌",
    "宮城（山形・福島含む）", "埼玉", "千葉", "東京", "神奈川",
    "愛知（三重・岐阜含む）", "大阪（京都・兵庫・奈良・和歌山含む）",
    "広島", "福岡（佐賀・大分・熊本含む）",
]

# navi の都道府県表記 → 固定エリアラベル。対応付けできない地域は None
# （候補ステージング行き）。「札幌」は市単位のラベルのため北海道全域は不可。
AREA_MAP = {
    "全国": "全国",
    "宮城": "宮城（山形・福島含む）", "山形": "宮城（山形・福島含む）",
    "福島": "宮城（山形・福島含む）",
    "埼玉": "埼玉", "千葉": "千葉", "東京": "東京", "神奈川": "神奈川",
    "愛知": "愛知（三重・岐阜含む）", "三重": "愛知（三重・岐阜含む）",
    "岐阜": "愛知（三重・岐阜含む）",
    "大阪": "大阪（京都・兵庫・奈良・和歌山含む）",
    "京都": "大阪（京都・兵庫・奈良・和歌山含む）",
    "兵庫": "大阪（京都・兵庫・奈良・和歌山含む）",
    "奈良": "大阪（京都・兵庫・奈良・和歌山含む）",
    "和歌山": "大阪（京都・兵庫・奈良・和歌山含む）",
    "広島": "広島",
    "福岡": "福岡（佐賀・大分・熊本含む）",
    "佐賀": "福岡（佐賀・大分・熊本含む）",
    "大分": "福岡（佐賀・大分・熊本含む）",
    "熊本": "福岡（佐賀・大分・熊本含む）",
}

SHOP_GROUPS = {
    "ゲオ": "geo", "GEO": "geo", "ゲオオンライン": "geo",
    "ポケモンセンターオンライン": "pokecen",
}

NAVI_URL = "https://pokeca-navi.jp/lotteries/"
POKECEN_NEWS = "https://www.pokemoncenter-online.com/news/?id={}"
OFFICIAL_INFO_URL = "https://www.pokemon-card.com/info/"

# 直接パースはしない小売（到達性プローブのみ）
RETAILER_PROBES = {
    "geo": "https://geo-online.co.jp/",
    "yodobashi": "https://www.yodobashi.com/",
    "biccamera": "https://www.biccamera.com/bc/main/",
    "rakuten-books": "https://books.rakuten.co.jp/",
    "amiami": "https://www.amiami.jp/",
}

STOP_TOKENS = {"ポケモンカードゲーム", "ポケモンカード", "ポケカ", "商品",
               "拡張パック", "カード", "の抽選販売について", "抽選販売"}


def now_jst() -> datetime:
    return datetime.now(JST)


def fetch(url: str, timeout: int = 25):
    """(status, text) を返す。HTTPエラーは status のみ、通信断は (None, '')"""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return None, ""


def strip_text(html_src: str) -> str:
    txt = re.sub(r"<script.*?</script>", "", html_src, flags=re.S)
    txt = re.sub(r"<style.*?</style>", "", txt, flags=re.S)
    txt = re.sub(r"<[^>]+>", "\n", txt)
    txt = html.unescape(txt)
    return "\n".join(l.strip() for l in txt.split("\n") if l.strip())


def dump_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def write_if_changed(path: Path, data) -> bool:
    s = dump_json(data)
    if path.exists() and path.read_text(encoding="utf-8") == s:
        return False
    path.write_text(s, encoding="utf-8")
    return True


# ---------- 日時パース ----------

def infer_year(month: int, day: int, today: date) -> date:
    d = date(today.year, month, day)
    if d < today - timedelta(days=1):
        d = date(today.year + 1, month, day)
    return d


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def make_dt(d: date, hh: int = 23, mm: int = 59) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=JST)


RANGE_PATTERNS = [
    # 2026年11月27日(金) 10:00 ～ 2026年11月29日(日) 23:59
    (r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})"
     r"\s*[～〜~]\s*"
     r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})",
     "ymd_hm"),
    # 2026年11月27日（金）16時00分から2026年11月30日（月）16時59分まで
    (r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\([^)]*\))?\s*(\d{1,2})時(\d{2})分から"
     r"\s*(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日(?:\([^)]*\))?\s*(\d{1,2})時(\d{2})分まで",
     "ymd_hm_kara"),
    # 11月30日11時～12月3日17時59分
    (r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2})時(?:(\d{1,2})分)?"
     r"\s*[～〜~]\s*"
     r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2})時(?:(\d{1,2})分)?",
     "md_hm"),
    # 11月27日(金) 10:00 ～ 11月29日(日) 23:59 （年なし・コロン形式）
    (r"(\d{1,2})月(\d{1,2})日(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})"
     r"\s*[～〜~]\s*"
     r"(\d{1,2})月(\d{1,2})日(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})",
     "md_hm"),
]


def parse_datetime_range(text: str, today: date):
    """日本語の期間表記から (start_dt, end_dt) を抽出。無ければ (None, None)"""
    for pat, kind in RANGE_PATTERNS:
        m = re.search(pat, text)
        if not m:
            continue
        g = m.groups()
        try:
            if kind == "ymd_hm":
                s = datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3]),
                             int(g[4]), tzinfo=JST)
                e = datetime(int(g[5]), int(g[6]), int(g[7]), int(g[8]),
                             int(g[9]), tzinfo=JST)
            elif kind == "ymd_hm_kara":
                sy = int(g[0])
                s = datetime(sy, int(g[1]), int(g[2]), int(g[3]), int(g[4]),
                             tzinfo=JST)
                ey = int(g[5]) if g[5] else sy
                e = datetime(ey, int(g[6]), int(g[7]), int(g[8]), int(g[9]),
                             tzinfo=JST)
            else:  # md_hm
                sd = infer_year(int(g[0]), int(g[1]), today)
                ed = infer_year(int(g[4]), int(g[5]), today)
                if ed < sd:
                    ed = date(ed.year + 1, ed.month, ed.day)
                s = datetime.combine(sd, time(int(g[2]), int(g[3] or 0)),
                                     tzinfo=JST)
                e = datetime.combine(ed, time(int(g[6]), int(g[7] or 0)),
                                     tzinfo=JST)
        except ValueError:
            continue
        if e >= s:
            return s, e
    return None, None


def parse_single_date(text: str, today: date):
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        return infer_year(int(m.group(1)), int(m.group(2)), today)
    return None


def parse_purchase_deadline(notes: str, today: date):
    m = re.search(r"購入期限は\s*(\d{1,2})月(\d{1,2})日", notes)
    if m:
        return make_dt(infer_year(int(m.group(1)), int(m.group(2)), today))
    m = re.search(r"購入期間は\s*(\d{1,2})月(\d{1,2})日\s*[～〜~]\s*"
                  r"(?:(\d{1,2})月)?(\d{1,2})日", notes)
    if m:
        em = int(m.group(3)) if m.group(3) else int(m.group(1))
        return make_dt(infer_year(em, int(m.group(4)), today))
    return None


def parse_result_date(notes: str, today: date):
    m = re.search(r"(?:結果発表|当選発表|結果は|発表日)[^。]{0,20}?"
                  r"(\d{1,2})月(\d{1,2})日", notes)
    if m:
        return infer_year(int(m.group(1)), int(m.group(2)), today).isoformat()
    return None


# ---------- 重複判定 ----------

def product_tokens(product: str):
    raw = re.split(r"[／/・「」『』（）()\s　¥,，]+", product)
    toks = set()
    for t in raw:
        t = t.strip()
        if t and t not in STOP_TOKENS:
            toks.add(t)
    return toks


def shop_group(shop: str) -> str:
    return SHOP_GROUPS.get(shop, shop)


def matches_existing(cand: dict, existing: list) -> bool:
    """候補が既存エントリと同一の抽選かを判定（店舗+商品+締切日）"""
    ctoks = product_tokens(cand.get("product") or "")
    cend = (cand.get("apply_end") or "")[:10]
    for it in existing:
        if shop_group(it.get("shop", "")) != shop_group(cand.get("shop", "")):
            continue
        itoks = product_tokens(it.get("product") or "")
        shared = {t for t in ctoks & itoks if len(t) >= 4}
        if not shared and len(ctoks & itoks) < 2:
            continue
        iend = (it.get("apply_end") or "")[:10]
        if cend and iend and cend != iend:
            # 締切日が違う場合は、応募期間が重なれば同一とみなす
            cs, is_ = cand.get("apply_start") or "", it.get("apply_start") or ""
            if not (cs and is_ and cs[:10] <= iend and is_[:10] <= cend):
                continue
        return True
    return False


# ---------- ポケカ抽選図鑑 (navi) ----------

def parse_navi(today: date):
    """navi の受付中一覧から候補 dict のリストを返す"""
    status, src = fetch(NAVI_URL)
    if status != 200:
        return [], f"status={status}"
    out = []
    chunks = src.split("lottery-card-open")
    for chunk in chunks[1:]:
        chunk = chunk[:16000]
        urls = [u for u in re.findall(r'href="([^"]+)"', chunk)
                if u.startswith("http") and "pokeca-navi" not in u
                and "googletagmanager" not in u]
        text = re.sub(r"<[^>]+>", "|", chunk)
        text = html.unescape(text)
        toks = [t.strip() for t in text.split("|") if t.strip()]
        # 分割位置の残骸（'"> ' 等、文字を含まないトークン）を先頭から除去
        while toks and not re.search(r"[0-9A-Za-zぁ-んァ-ヶ一-龥]", toks[0]):
            toks.pop(0)
        if len(toks) < 5:
            continue
        shop, product = toks[0], toks[1]
        area_raw = toks[2]
        rest = toks[3:]
        method = rest[0] if rest and ("フォーム" in rest[0]
                                      or "リポスト" in rest[0]
                                      or "店頭" in rest[0]) else ""
        # 締切 M/D
        end_date = None
        for t in rest:
            m = re.fullmatch(r"(\d{1,2})/(\d{1,2})\((.)\)", t)
            if m:
                end_date = infer_year(int(m.group(1)), int(m.group(2)), today)
                break
        notes = ""
        if "申込方法の要点" in rest:
            i = rest.index("申込方法の要点")
            if i + 1 < len(rest):
                notes = rest[i + 1]
        out.append({
            "source": "pokeca-navi",
            "source_url": NAVI_URL,
            "shop": shop,
            "product": product,
            "area_raw": area_raw,
            "method": method,
            "end_date": end_date,
            "notes": notes,
            "url": urls[0] if urls else "",
        })
    return out, f"cards={len(out)}"


def navi_to_entry(c: dict, today: date):
    """navi 候補を (entry, candidate) のどちらかに振り分ける"""
    missing = []
    notes = c["notes"] or ""
    # 期間: 要点に完全な期間があればそれを優先、無ければ締切日のみ
    s_dt, e_dt = parse_datetime_range(notes, today)
    end_time_known = e_dt is not None
    if e_dt is None and c["end_date"] is not None:
        e_dt = make_dt(c["end_date"])
    if e_dt is None:
        missing.append("応募期間（締切日）")
    area = AREA_MAP.get(c["area_raw"])
    if area is None:
        missing.append(f"エリア（{c['area_raw']} は固定ラベルに該当なし）")
    if not c["url"]:
        missing.append("応募URL")
    # プレゼントキャンペーンらしきものは分類確認のため候補行き
    if re.search(r"当たるキャンペーン|賞品は|名に当たります|名様に当たります"
                 r"|レシート.{0,12}応募", notes) \
            and "抽選販売" not in notes:
        missing.append("分類（抽選販売ではなくプレゼントキャンペーンの可能性）")
    base = {
        "shop": c["shop"],
        "product": c["product"],
        "area": area,
        "apply_start": iso(s_dt) if s_dt else None,
        "apply_end": iso(e_dt) if e_dt else None,
        "result_date": parse_result_date(notes, today),
        "purchase_deadline": (iso(parse_purchase_deadline(notes, today))
                              if parse_purchase_deadline(notes, today) else None),
        "url": c["url"] or None,
    }
    if missing:
        cand = dict(base)
        cand.update({"source": c["source"], "source_url": c["source_url"],
                     "missing": missing, "excerpt": notes[:300],
                     "found_at": today.isoformat()})
        cand["area"] = area or c["area_raw"]
        return None, cand
    conditions = notes
    if not end_time_known:
        conditions = (conditions + "（締切時刻は未確認のため23:59表示）").strip()
    entry = dict(base)
    entry["id"] = "watch-" + hashlib.sha1(
        f"{c['shop']}|{c['product']}|{base['apply_end']}".encode()
    ).hexdigest()[:10]
    entry["conditions"] = conditions or None
    return entry, None


# ---------- ポケモンセンターオンライン ----------

def parse_pokecen(news_id: str, today: date):
    """詳細ページを解析。(kind, payload) を返す
    kind: 'entry' | 'candidate' | 'not_lottery' | 'fetch_failed'"""
    url = POKECEN_NEWS.format(news_id)
    status, src = fetch(url)
    if status != 200:
        return "fetch_failed", {"status": status}
    title_m = re.search(r"<title>(.*?)</title>", src, re.S)
    title = html.unescape(title_m.group(1)) if title_m else ""
    body = strip_text(src)
    if "抽選販売" not in title or "ポケモンカード" not in title:
        return "not_lottery", {}
    # タイトル内の期間（日付のみ）: 12月1日（火）～12月7日（月）
    tw_s = tw_e = None
    m = re.search(r"(\d{1,2})月(\d{1,2})日（.）[～〜~](\d{1,2})月(\d{1,2})日（.）",
                  title)
    if m:
        tw_s = infer_year(int(m.group(1)), int(m.group(2)), today)
        tw_e = infer_year(int(m.group(3)), int(m.group(4)), today)
    # 本文テキストから時刻付きの完全な期間を探す
    s_dt, e_dt = parse_datetime_range(body, today)
    product = re.sub(r"の(追加)?抽選販売について.*$", "", title)
    product = re.sub(r"^\d{1,2}月\d{1,2}日（.）[～〜~]\d{1,2}月\d{1,2}日（.）",
                     "", product).strip()
    result = None
    rm = re.search(r"抽選結果発表日[^0-9]{0,10}(\d{4})年(\d{1,2})月(\d{1,2})日",
                   body)
    if rm:
        result = date(int(rm.group(1)), int(rm.group(2)),
                      int(rm.group(3))).isoformat()
    base = {"shop": "ポケモンセンターオンライン", "product": product,
            "area": "全国", "url": url,
            "apply_start": iso(s_dt) if s_dt else None,
            "apply_end": iso(e_dt) if e_dt else None,
            "result_date": result, "purchase_deadline": None}
    missing = []
    if s_dt is None or e_dt is None:
        missing.append("応募期間の時刻（期間詳細は画像掲載のため未抽出）")
    if result is None:
        missing.append("結果発表日")
    missing.append("購入期限")
    if s_dt is not None and e_dt is not None and result is not None:
        entry = dict(base)
        entry["id"] = f"pokecen-{news_id}"
        entry["conditions"] = None
        return "entry", entry
    cand = dict(base)
    cand.update({"source": "pokemoncenter-online", "source_url": url,
                 "missing": missing,
                 "title_window": [tw_s.isoformat() if tw_s else None,
                                  tw_e.isoformat() if tw_e else None],
                 "excerpt": title[:200], "found_at": today.isoformat()})
    return "candidate", cand


# ---------- メイン ----------

def main() -> int:
    today = now_jst().date()
    lotteries = load_json(LOTTERIES_PATH, {"updated_at": None, "items": []})
    candidates = load_json(CANDIDATES_PATH, {"updated_at": None, "items": []})
    state = load_json(STATE_PATH, None)
    if state is None:
        state = {"version": 1, "seen": {}, "pokecen_last_probed": None,
                 "source_status": {}}
    seen = state.setdefault("seen", {})
    log = []

    def mark(key, disposition):
        if key not in seen:
            seen[key] = {"disposition": disposition,
                         "at": today.isoformat()}

    new_entries, new_cands = [], []

    # --- navi ---
    navi_cards, navi_info = [], ""
    try:
        navi_cards, navi_info = parse_navi(today)
    except Exception as e:  # ソース単位で失敗を隔離
        navi_info = f"error {type(e).__name__}"
    n_stat = {"added": 0, "candidates": 0, "known": 0, "seen": 0}
    pokecen_ids_from_links = set()
    for c in navi_cards:
        if c["source_url"] and "pokemoncenter-online.com/news/?id=" in c["url"]:
            m = re.search(r"id=(\d+)", c["url"])
            if m:
                pokecen_ids_from_links.add(m.group(1))
        key = (f"navi:{c['shop']}|{c['product']}|"
               f"{c['end_date'].isoformat() if c['end_date'] else ''}")
        if key in seen:
            n_stat["seen"] += 1
            continue
        entry, cand = navi_to_entry(c, today)
        probe = entry or cand
        if probe and matches_existing(probe, lotteries["items"]):
            mark(key, "known")
            n_stat["known"] += 1
            continue
        if entry:
            new_entries.append(entry)
            mark(key, "added")
            n_stat["added"] += 1
        elif cand:
            new_cands.append(cand)
            mark(key, "candidate")
            n_stat["candidates"] += 1
    log.append(f"[watch] navi: {navi_info} added={n_stat['added']} "
               f"candidates={n_stat['candidates']} known={n_stat['known']} "
               f"seen_skip={n_stat['seen']}")

    # --- 公式お知らせ（ポケセン id の発見） ---
    info_ids = set()
    try:
        st, src = fetch(OFFICIAL_INFO_URL)
    except Exception:
        st, src = None, ""
    if st == 200:
        for m in re.finditer(
                r"pokemoncenter-online\.com/news/\?id=(\d+)", src):
            info_ids.add(m.group(1))
    log.append(f"[watch] official-info: status={st} "
               f"pokecen_ids={len(info_ids)}")

    # --- ポケセン: 発見済み id + 日付プローブ ---
    probe_dates = []
    last = state.get("pokecen_last_probed")
    start = (date.fromisoformat(last) + timedelta(days=1)) if last \
        else today - timedelta(days=2)
    if start < today - timedelta(days=13):
        start = today - timedelta(days=13)
    d = start
    while d <= today:
        probe_dates.append(d)
        d += timedelta(days=1)
    pokecen_targets = {}
    for news_id in sorted(pokecen_ids_from_links | info_ids):
        # 30日より古い告知は対象外（終了済み。既知判定のノイズになる）
        try:
            id_date = datetime.strptime(news_id, "%Y%m%d").date()
        except ValueError:
            continue
        if id_date < today - timedelta(days=30):
            mark(f"pokecen:{news_id}", "old")
            continue
        pokecen_targets[news_id] = "link"
    for d in probe_dates:
        pokecen_targets.setdefault(d.strftime("%Y%m%d"), "probe")
    p_stat = {"added": 0, "candidates": 0, "known": 0, "seen": 0,
              "not_lottery": 0, "missing_pages": 0}
    host_down = False
    for news_id, origin in sorted(pokecen_targets.items()):
        key = f"pokecen:{news_id}"
        if key in seen:
            p_stat["seen"] += 1
            continue
        kind, payload = parse_pokecen(news_id, today)
        if kind == "fetch_failed":
            if payload.get("status") is None:
                host_down = True
            else:
                p_stat["missing_pages"] += 1
                mark(key, "no_page")
            continue
        if kind == "not_lottery":
            mark(key, "not_lottery")
            p_stat["not_lottery"] += 1
            continue
        if matches_existing(payload, lotteries["items"]) or \
                pokecen_matches_known(payload, lotteries["items"]):
            mark(key, "known")
            p_stat["known"] += 1
            continue
        if kind == "entry":
            new_entries.append(payload)
            mark(key, "added")
            p_stat["added"] += 1
        else:
            new_cands.append(payload)
            mark(key, "candidate")
            p_stat["candidates"] += 1
    if not host_down and probe_dates:
        state["pokecen_last_probed"] = today.isoformat()
    log.append(f"[watch] pokecen: targets={len(pokecen_targets)} "
               f"added={p_stat['added']} candidates={p_stat['candidates']} "
               f"known={p_stat['known']} seen_skip={p_stat['seen']} "
               f"not_lottery={p_stat['not_lottery']} "
               f"no_page={p_stat['missing_pages']}")

    # --- 小売の到達性プローブ（パース対象外・navi 経由でカバー） ---
    r_parts = []
    for name, url in RETAILER_PROBES.items():
        st, _ = fetch(url, timeout=10)
        r_parts.append(f"{name}={st if st else 'timeout'}")
        # 生ステータスは揺れるため、粗いカテゴリでだけ状態に残す
        cat = "ok" if st == 200 else "blocked" if st == 403 \
            else "unreachable"
        prev = state.setdefault("source_status", {}).get(name, {})
        if prev.get("status") != cat:
            state["source_status"][name] = {
                "status": cat, "at": today.isoformat(),
                "note": "announcement feed not parseable; covered via navi"}
    log.append("[watch] retailers (probe only): " + " ".join(r_parts))

    # --- マージ＆書き出し（変化がある時だけ書く） ---
    if new_entries:
        lotteries["items"].extend(new_entries)
        lotteries["updated_at"] = today.isoformat()
    # 候補: 既存候補と重複しないものだけ追加し、登録済みになった候補は除く
    cand_keys = {(c.get("source_url"), c.get("product"))
                 for c in candidates["items"]}
    added_cand = 0
    for c in new_cands:
        k = (c.get("source_url"), c.get("product"))
        if k not in cand_keys:
            candidates["items"].append(c)
            cand_keys.add(k)
            added_cand += 1
    before = len(candidates["items"])
    candidates["items"] = [
        c for c in candidates["items"]
        if not matches_existing(c, lotteries["items"])]
    if len(candidates["items"]) != before or added_cand:
        candidates["updated_at"] = today.isoformat()

    w1 = write_if_changed(LOTTERIES_PATH, lotteries)
    w2 = write_if_changed(CANDIDATES_PATH, candidates)
    w3 = write_if_changed(STATE_PATH, state)
    log.append(f"[watch] lotteries: items={len(lotteries['items'])} "
               f"(+{len(new_entries)}) written={w1}; "
               f"candidates: items={len(candidates['items'])} "
               f"(+{added_cand}) written={w2}; state written={w3}")
    print("\n".join(log))
    return 0


def pokecen_matches_known(payload: dict, existing: list) -> bool:
    """ポケセン候補の既知判定: タイトルの期間（日付）が既存の応募期間と
    一致するか、商品名の特徴トークンが重なれば既知とみなす"""
    tw = payload.get("title_window") or [None, None]
    for it in existing:
        if shop_group(it.get("shop", "")) != "pokecen":
            continue
        if tw[0] and tw[1] and (it.get("apply_start") or "")[:10] == tw[0] \
                and (it.get("apply_end") or "")[:10] == tw[1]:
            return True
        shared = {t for t in product_tokens(payload.get("product") or "")
                  & product_tokens(it.get("product") or "") if len(t) >= 5}
        if shared:
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())

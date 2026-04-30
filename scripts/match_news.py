#!/usr/bin/env python3
"""ニュースエントリを config/coins.yaml の銘柄でフィルタする。

入力: inputs/raw/<date>/news.json(fetch_rss.py の出力)
出力: inputs/matches/<TICKER>/matched.json と matched.md(ヒット銘柄ごと)

スコアリングは alias / context / theme の重み付き合計と、
クロスマーケット記事の検出に基づく減点で構成される。
判定ロジックは evaluate_entry() に集約してあり、
重みとしきい値は WEIGHTS / ACCEPT_THRESHOLD 定数で調整できる。
棄却された記事は rejected_items[] に why_matched 付きで残し、
ハンズオン中のしきい値調整・辞書改善に利用できるようにする。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COINS_PATH = REPO_ROOT / "config" / "coins.yaml"
RAW_BASE = REPO_ROOT / "inputs" / "raw"
MATCH_BASE = REPO_ROOT / "inputs" / "matches"

# シグナル別の重み。コーパスを見ながらここで調整する。
WEIGHTS: dict[str, int] = {
    "title_context":   4,
    "title_alias":     3,
    "title_theme":     2,
    "summary_context": 2,
    "summary_alias":   1,
    "summary_theme":   1,
    "cross_market_penalty": -3,
}
# このスコア未満の記事は rejected_items[] に振り分ける。
ACCEPT_THRESHOLD = 3
# タイトルにこの数以上の銘柄 alias がヒットすればまとめ記事と判定する。
CROSS_MARKET_COIN_THRESHOLD = 3


# --- 設定読み込み -------------------------------------------------------------


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_news(date: str) -> dict[str, Any]:
    path = RAW_BASE / date / "news.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# --- マッチャー ---------------------------------------------------------------

_ASCII_ONLY = re.compile(r"^[\x00-\x7f]+$")


def _compile_term(term: str) -> re.Pattern[str]:
    """ASCII 英数字のみの語は \\b 境界で囲む。それ以外(記号・空白入り・非 ASCII)は素マッチ。"""
    if _ASCII_ONLY.match(term) and term.isalnum():
        return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    return re.compile(re.escape(term), re.IGNORECASE)


def compile_coins(coins_cfg: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """coins.yaml の各銘柄に対して alias / theme / context をコンパイルする。"""
    compiled: list[dict[str, Any]] = []
    for coin in coins_cfg:
        if not coin.get("enabled", True):
            continue
        compiled.append(
            {
                "ticker": coin["ticker"],
                "name": coin.get("name", coin["ticker"]),
                "aliases": [(a, _compile_term(a)) for a in (coin.get("aliases") or [])],
                "themes":  [(t, _compile_term(t)) for t in (coin.get("themes") or [])],
                "context": [(c, _compile_term(c)) for c in (coin.get("required_context_terms") or [])],
            }
        )
    return compiled


def _scan(text: str, terms: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    """term リストを text に対して全件チェックし、ヒットした原語を順序保持で重複排除して返す。"""
    hits: list[str] = []
    for raw, pattern in terms:
        if pattern.search(text) and raw not in hits:
            hits.append(raw)
    return hits


def cross_market_info(entry: dict[str, Any], coins: list[dict[str, Any]]) -> dict[str, Any]:
    """タイトルに alias がヒットする銘柄の数と、最も早い出現位置の銘柄(主題)を返す。"""
    title = entry.get("title") or ""
    hits: list[tuple[int, str]] = []
    for coin in coins:
        earliest: int | None = None
        for _raw, pattern in coin["aliases"]:
            m = pattern.search(title)
            if m and (earliest is None or m.start() < earliest):
                earliest = m.start()
        if earliest is not None:
            hits.append((earliest, coin["ticker"]))
    if not hits:
        return {"count": 0, "primary_ticker": None}
    hits.sort()
    return {"count": len(hits), "primary_ticker": hits[0][1]}


def evaluate_entry(
    entry: dict[str, Any],
    coin: dict[str, Any],
    cm: dict[str, Any],
) -> dict[str, Any] | None:
    """1 entry × 1 coin の重み付きスコアを計算する。

    どこにも alias / context / theme がヒットしなければ None。
    返り値の verdict は score >= ACCEPT_THRESHOLD で決まる。
    """
    title = entry.get("title") or ""
    summary = entry.get("summary") or ""

    title_aliases   = _scan(title,   coin["aliases"])
    summary_aliases = _scan(summary, coin["aliases"])

    # alias ゲート: 銘柄名そのもの(alias)が title または summary に
    # 1 つもヒットしていない記事は採点しない。themes / context のみのヒット
    # では採用しない方針(generic 文脈語による誤検知を遮断するため)。
    if not (title_aliases or summary_aliases):
        return None

    title_themes    = _scan(title,   coin["themes"])
    summary_themes  = _scan(summary, coin["themes"])
    title_context   = _scan(title,   coin["context"])
    summary_context = _scan(summary, coin["context"])

    signals: list[dict[str, Any]] = []
    score = 0

    def add(signal_name: str, terms: list[str]) -> None:
        nonlocal score
        pts = WEIGHTS[signal_name]
        for term in terms:
            signals.append({"signal": signal_name, "term": term, "points": pts})
            score += pts

    add("title_alias",     title_aliases)
    add("summary_alias",   summary_aliases)
    add("title_context",   title_context)
    add("summary_context", summary_context)
    add("title_theme",     title_themes)
    add("summary_theme",   summary_themes)

    is_primary = (cm["primary_ticker"] == coin["ticker"])
    has_context = bool(title_context or summary_context)
    cross_market_apply = (
        cm["count"] >= CROSS_MARKET_COIN_THRESHOLD
        and not is_primary
        and not has_context
    )
    if cross_market_apply:
        pts = WEIGHTS["cross_market_penalty"]
        signals.append(
            {
                "signal": "cross_market_penalty",
                "points": pts,
                "reason": f"{cm['count']} coins in title; not primary; no context hit",
            }
        )
        score += pts

    matched_terms = sorted(
        {
            *title_aliases, *summary_aliases,
            *title_themes,  *summary_themes,
            *title_context, *summary_context,
        }
    )

    return {
        "score": score,
        "verdict": "kept" if score >= ACCEPT_THRESHOLD else "rejected",
        "matched_terms": matched_terms,
        "matched_in": {
            "title":   {"aliases": title_aliases,   "themes": title_themes,   "context": title_context},
            "summary": {"aliases": summary_aliases, "themes": summary_themes, "context": summary_context},
        },
        "is_primary_subject": is_primary,
        "cross_market_coin_count": cm["count"],
        "signals": signals,
    }


# --- 出力 ---------------------------------------------------------------------


def render_markdown(bucket: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# {bucket['ticker']} — {bucket['name']}")
    lines.append("")
    lines.append(f"- Source news: `{bucket['source_news_path']}`")
    lines.append(f"- Matched at: {bucket['matched_at']}")
    lines.append(f"- Match count: {bucket['match_count']} (rejected: {bucket['rejected_count']})")
    lines.append("")

    for i, item in enumerate(bucket["items"], 1):
        title = item["title"] or "(no title)"
        url = item["url"] or "#"
        lines.append(f"## {i}. [{title}]({url})")
        lines.append("")
        lines.append(f"- Source: {item['source_name']} (`{item['source']}`)")
        if item.get("published_at"):
            lines.append(f"- Published: {item['published_at']}")
        if item.get("matched_terms"):
            lines.append(f"- Matched terms: {', '.join(item['matched_terms'])}")
        lines.append(f"- Score: {item['score']}")
        lines.append("")
        if item.get("summary"):
            lines.append(item["summary"])
            lines.append("")
        lines.append("---")
        lines.append("")

    if bucket["rejected_items"]:
        lines.append("## 棄却記事(参考)")
        lines.append("")
        lines.append("以下はしきい値未満で棄却された記事です。スコアリング調整時の参考に残しています。")
        lines.append("")
        for item in bucket["rejected_items"]:
            title = item["title"] or "(no title)"
            url = item["url"] or "#"
            score = item["score"]
            terms = ", ".join(item.get("matched_terms") or [])
            why = item.get("why_matched", {})
            penalty = ""
            for sig in why.get("signals", []):
                if sig.get("signal") == "cross_market_penalty":
                    penalty = " [cross-market]"
                    break
            lines.append(
                f"- (score={score}{penalty}) [{title}]({url}) — {item['source_name']}, ヒット: {terms}"
            )
        lines.append("")

    return "\n".join(lines)


def write_bucket(bucket: dict[str, Any]) -> tuple[Path, Path]:
    out_dir = MATCH_BASE / bucket["ticker"]
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "matched.json"
    md_path = out_dir / "matched.md"
    json_path.write_text(json.dumps(bucket, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(bucket), encoding="utf-8")
    return json_path, md_path


def _sorted_by_pub(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """published_at 降順、日時不明は末尾。"""
    with_pub = sorted(
        (i for i in items if i.get("published_at")),
        key=lambda i: i["published_at"],
        reverse=True,
    )
    without_pub = [i for i in items if not i.get("published_at")]
    return with_pub + without_pub


# --- メイン -------------------------------------------------------------------


def main(argv: list[str]) -> int:
    date = argv[1] if len(argv) > 1 else datetime.now().strftime("%Y-%m-%d")

    try:
        news = load_news(date)
    except FileNotFoundError:
        print(f"news.json が見つかりません: {RAW_BASE / date / 'news.json'}", file=sys.stderr)
        print("先に scripts/fetch_rss.py を実行してください。", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"news.json のパースに失敗: {e}", file=sys.stderr)
        return 1

    try:
        coins_raw = load_yaml(COINS_PATH).get("coins") or []
    except (FileNotFoundError, yaml.YAMLError) as e:
        print(f"coins.yaml の読み込みに失敗: {e}", file=sys.stderr)
        return 1

    coins = compile_coins(coins_raw)
    if not coins:
        print("有効な銘柄がありません。config/coins.yaml を確認してください。", file=sys.stderr)
        return 1

    id_to_name = {s["id"]: s.get("name", s["id"]) for s in news.get("sources", [])}

    entries = news.get("entries", [])
    source_news_rel = str((RAW_BASE / date / "news.json").relative_to(REPO_ROOT))
    matched_at = datetime.now(timezone.utc).astimezone().isoformat()

    buckets: dict[str, dict[str, list[dict[str, Any]]]] = {
        c["ticker"]: {"items": [], "rejected": []} for c in coins
    }

    for entry in entries:
        cm = cross_market_info(entry, coins)
        for coin in coins:
            result = evaluate_entry(entry, coin, cm)
            if result is None:
                continue
            source_id = entry.get("source", "")
            item_record = {
                "source": source_id,
                "source_name": id_to_name.get(source_id, source_id),
                "title": entry.get("title", ""),
                "url": entry.get("url", ""),
                "summary": entry.get("summary", ""),
                "published_at": entry.get("published_at"),
                "score": result["score"],
                "matched_terms": result["matched_terms"],
                "matched_in": result["matched_in"],
                "why_matched": {
                    "verdict": result["verdict"],
                    "is_primary_subject": result["is_primary_subject"],
                    "cross_market_coin_count": result["cross_market_coin_count"],
                    "signals": result["signals"],
                },
            }
            if result["verdict"] == "kept":
                buckets[coin["ticker"]]["items"].append(item_record)
            else:
                buckets[coin["ticker"]]["rejected"].append(item_record)

    written: list[Path] = []
    for coin in coins:
        items = _sorted_by_pub(buckets[coin["ticker"]]["items"])
        rejected = _sorted_by_pub(buckets[coin["ticker"]]["rejected"])

        if not items and not rejected:
            print(f"[{coin['ticker']}] ヒットなし — スキップ", file=sys.stderr)
            continue
        if not items:
            print(
                f"[{coin['ticker']}] 採択 0 件、棄却 {len(rejected)} 件 — スキップ",
                file=sys.stderr,
            )
            continue

        bucket = {
            "ticker": coin["ticker"],
            "name": coin["name"],
            "source_news_path": source_news_rel,
            "matched_at": matched_at,
            "match_count": len(items),
            "rejected_count": len(rejected),
            "items": items,
            "rejected_items": rejected,
        }
        json_path, md_path = write_bucket(bucket)
        print(
            f"[{coin['ticker']}] 採択 {len(items)} 件、棄却 {len(rejected)} 件 → {json_path.name}, {md_path.name}",
            file=sys.stderr,
        )
        written.append(json_path)
        written.append(md_path)

    if not written:
        print("どの銘柄にもヒットしませんでした。", file=sys.stderr)
        return 0

    for p in written:
        print(str(p))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

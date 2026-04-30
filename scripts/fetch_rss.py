#!/usr/bin/env python3
"""RSS フィードを取得して inputs/raw/YYYY-MM-DD/ に news.json と news.md を書き出す。

config/rss_sources.yaml の sources を順に処理する。
1 ソースの失敗で全体を止めないよう、ソース単位で例外を握る。
本文の追加スクレイピングはここでは行わない(RSS の summary までで完結する)。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests
import yaml
from dateutil import parser as dateparser

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "rss_sources.yaml"
OUTPUT_BASE = REPO_ROOT / "inputs" / "raw"

# 設定値の UA で 403/406/429 を返す媒体に対するフォールバック用のブラウザ風 UA。
# 通常はカスタム UA(クローラ識別)を尊重し、CDN が弾いたときだけこちらに切り替える。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
RETRY_STATUSES = {403, 406, 429}


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def normalize_published(entry: Any) -> str | None:
    """エントリから公開日時を ISO8601(UTC)文字列で返す。失敗したら None。"""
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if not val:
            continue
        try:
            dt = dateparser.parse(val)
        except (ValueError, TypeError, OverflowError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return None


def is_within_lookback(published_iso: str | None, cutoff: datetime) -> bool:
    """published_at が cutoff 以降なら True。日時が無い/壊れているエントリは救済として True。"""
    if not published_iso:
        return True
    try:
        return dateparser.parse(published_iso) >= cutoff
    except (ValueError, TypeError, OverflowError):
        return True


def _http_get(
    url: str, headers: dict[str, str], timeout: int, display_name: str
) -> tuple[requests.Response | None, str | None]:
    """HTTP GET を行い、レスポンスと診断ログを返す。例外は文字列エラーに畳む。"""
    try:
        response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        return None, f"取得失敗: {e!r}"
    print(
        f"[{display_name}] HTTP {response.status_code} "
        f"Content-Type={response.headers.get('Content-Type', '?')!r} "
        f"final_url={response.url} "
        f"bytes={len(response.content)}",
        file=sys.stderr,
    )
    return response, None


def fetch_source(source: dict[str, Any], user_agent: str, timeout: int) -> tuple[list[dict], str | None]:
    """1 ソース分のエントリを取得する。失敗時は (空リスト, エラーメッセージ) を返す。

    HTTP 取得は requests に寄せて feedparser.parse(bytes) に渡す。
    feedparser に直接 URL を渡すと CDN の bot 判定で HTML が返り、SAX エラーになる媒体がある。
    """
    source_id = source.get("id", "unknown")
    display_name = source.get("name") or source_id
    url = source.get("url", "")

    if not url or url.startswith("TODO_"):
        return [], f"URL がプレースホルダのまま: {url!r}"

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    }

    response, error = _http_get(url, headers, timeout, display_name)
    if error:
        return [], error

    # CDN の bot 判定で 403/406/429 が返ったときだけ、ブラウザ風 UA で1回だけ再試行する。
    if response.status_code in RETRY_STATUSES:
        print(
            f"[{display_name}] HTTP {response.status_code} → ブラウザ風 UA で再試行",
            file=sys.stderr,
        )
        headers["User-Agent"] = BROWSER_USER_AGENT
        response, error = _http_get(url, headers, timeout, display_name)
        if error:
            return [], error

    if not response.ok:
        return [], f"HTTP {response.status_code} {response.reason}"

    parsed = feedparser.parse(response.content)

    # bozo はパース時の警告フラグ。エントリが取れていれば軽微な警告として通す。
    if parsed.bozo and not parsed.entries:
        return [], f"パース失敗: {parsed.bozo_exception!r}"

    entries: list[dict] = []
    for entry in parsed.entries:
        entries.append(
            {
                "source": source_id,
                "title": (entry.get("title") or "").strip(),
                "url": entry.get("link") or "",
                "summary": (entry.get("summary") or "").strip(),
                "published_at": normalize_published(entry),
            }
        )
    return entries, None


def render_markdown(entries: list[dict], summary: dict[str, Any]) -> str:
    # entries[].source は id(slug)で入っている
    by_source_id: dict[str, list[dict]] = {}
    for e in entries:
        by_source_id.setdefault(e["source"], []).append(e)

    lines: list[str] = []
    lines.append(f"# News — {summary['date']}")
    lines.append("")
    lines.append(f"- Fetched at: {summary['fetched_at']}")
    lines.append(f"- Lookback: {summary['lookback_hours']}h")
    lines.append(f"- Total entries: {len(entries)}")
    lines.append("")

    for src_meta in summary["sources"]:
        source_id = src_meta["id"]
        display_name = src_meta["name"]
        items = by_source_id.get(source_id, [])
        lines.append(f"## {display_name} ({len(items)} entries)")
        lines.append("")

        if src_meta.get("error"):
            lines.append(f"> 取得エラー: {src_meta['error']}")
            lines.append("")
            continue
        if not items:
            lines.append("> エントリなし")
            lines.append("")
            continue

        for item in items:
            title = item["title"] or "(no title)"
            url = item["url"] or "#"
            lines.append(f"### [{title}]({url})")
            if item["published_at"]:
                lines.append(f"_Published: {item['published_at']}_")
            lines.append("")
            if item["summary"]:
                lines.append(item["summary"])
                lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def main() -> int:
    try:
        config = load_config(CONFIG_PATH)
    except FileNotFoundError:
        print(f"設定ファイルが見つかりません: {CONFIG_PATH}", file=sys.stderr)
        return 1
    except yaml.YAMLError as e:
        print(f"YAML パース失敗: {e}", file=sys.stderr)
        return 1

    fetch_cfg = config.get("fetch") or {}
    user_agent = fetch_cfg.get("user_agent", "claude-code-practice/0.1")
    timeout = int(fetch_cfg.get("timeout_seconds", 15))
    lookback_hours = int(fetch_cfg.get("lookback_hours", 72))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    sources = [s for s in (config.get("sources") or []) if s.get("enabled", True)]

    if not sources:
        print("有効なソースがありません。config/rss_sources.yaml を確認してください。", file=sys.stderr)
        return 1

    all_entries: list[dict] = []
    source_results: list[dict] = []

    for src in sources:
        source_id = src.get("id", "unknown")
        display_name = src.get("name") or source_id
        print(f"[{display_name}] 取得中: {src.get('url')}", file=sys.stderr)

        entries, error = fetch_source(src, user_agent, timeout)
        if error:
            print(f"[{display_name}] エラー: {error}", file=sys.stderr)

        filtered = [e for e in entries if is_within_lookback(e["published_at"], cutoff)]
        all_entries.extend(filtered)
        source_results.append(
            {
                "id": source_id,
                "name": display_name,
                "url": src.get("url"),
                "error": error,
                "raw_count": len(entries),
                "entry_count": len(filtered),
            }
        )
        print(
            f"[{display_name}] 取得 {len(entries)} 件、{lookback_hours}h 以内 {len(filtered)} 件",
            file=sys.stderr,
        )

    today = datetime.now().strftime("%Y-%m-%d")
    fetched_at = datetime.now(timezone.utc).astimezone().isoformat()
    out_dir = OUTPUT_BASE / today

    summary: dict[str, Any] = {
        "date": today,
        "fetched_at": fetched_at,
        "lookback_hours": lookback_hours,
        "sources": source_results,
    }
    payload = {**summary, "entries": all_entries}

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / "news.json"
        md_path = out_dir / "news.md"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(render_markdown(all_entries, summary), encoding="utf-8")
    except OSError as e:
        print(f"出力失敗: {e}", file=sys.stderr)
        return 1

    print(str(json_path))
    print(str(md_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())

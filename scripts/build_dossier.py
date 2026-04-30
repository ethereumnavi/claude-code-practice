#!/usr/bin/env python3
"""matched.json を 1 銘柄 1 本の Markdown dossier にまとめる。

LLM 呼び出しはしない。matched.json と crypto news 向けキーワード辞書だけで
「今何が話題か」と論点整理(主流/対立/深掘り候補)を機械的に組み立てる。

設計:
- 6 テーマ × サブテーマ辞書による 2 段階分類(同テーマでも論点が分かれる場合は近接配置)
- 同一イベント(タイトル類似度 + 数値共有)を 1 ブロックに束ね、また同じイベント線で連結
- 件数の少ない銘柄(<4 件)はサブテーマ・クラスター化を無効にしてフラットに並べる
- 4 論点欄はクラスター単位で de-dup、タイトルヒットを要約ヒットより優先
"""
from __future__ import annotations

import html
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
MATCH_BASE = REPO_ROOT / "inputs" / "matches"
DOSSIER_BASE = REPO_ROOT / "inputs" / "dossiers"

OTHER_THEME = "その他"

# ── テーマ表示順 / タイブレーク優先順 ─────────────────────────────────────
THEME_DISPLAY_ORDER: list[str] = [
    "マクロ・市場環境",
    "政策・規制・制度",
    "機関投資家・ETF・企業財務",
    "決済・実需・ユースケース",
    "ネットワーク健全性・技術論点",
    "市場心理・ポジショニング",
]

THEME_TIEBREAK_PRIORITY: list[str] = [
    "ネットワーク健全性・技術論点",
    "政策・規制・制度",
    "決済・実需・ユースケース",
    "機関投資家・ETF・企業財務",
    "マクロ・市場環境",
    "市場心理・ポジショニング",
]

# ── 親テーマのキーワード辞書(分類用) ───────────────────────────────────
THEME_KEYWORDS: dict[str, list[str]] = {
    "マクロ・市場環境": [
        "macro", "macroeconomic",
        "fed", "federal reserve", "fomc", "rate cut", "rate hike", "interest rate",
        "inflation", "cpi", "ppi", "recession", "gdp",
        "oil", "opec", "brent", "wti", "crude",
        "gold", "commodity",
        "dollar", "dxy", "yen", "euro",
        "equity", "equities", "stocks", "s&p", "sp500", "nasdaq", "dow",
        "geopolitic", "war", "conflict",
        "iran", "china", "russia", "ukraine", "israel", "hormuz",
        "ai slowdown", "recession risk",
    ],
    "政策・規制・制度": [
        "regulation", "regulator", "regulatory",
        "sec", "cftc", "finra", "ofac", "fincen", "treasury department",
        "doj", "fbi", "prosecutor", "prosecution", "indict",
        "lawsuit", "court ruling", "settlement", "plea",
        "congress", "senate", "lawmaker", " bill ", "legislation",
        "white house", "executive order", "president",
        "license", "ban ", "prohibit", "sanction",
        "policy", "framework",
        "strategic reserve", "strategic bitcoin reserve", "sovereign",
        "prison", "sentence", "fraud", "scam", "money laundering", "aml",
    ],
    "機関投資家・ETF・企業財務": [
        "etf", "etfs", "etp",
        "institutional", "institution",
        "inflow", "outflow", "aum", "assets under management",
        "blackrock", "fidelity", "vanguard", "vaneck", "ark invest", "grayscale",
        "bernstein", "jpmorgan", "goldman", "citi", "td cowen", "benchmark",
        "microstrategy", "saylor", "strategy buys", "strategy adds", "strategy's",
        "strive", "smarter web", "marathon digital", "mara holdings", "riot", "bitmine",
        "consensys", "lubin", "joseph lubin", "defi united",
        "treasury vehicle", "corporate treasury", "treasury company", "bitcoin treasury",
        "publicly traded", "listed company", "ipo",
        "buy rating", "sell rating", "hold rating", "initiate", "reiterate", "target price",
        "earnings", "revenue",
        "acquires", "purchase", "buys ", "accumulate", "bought",
    ],
    "決済・実需・ユースケース": [
        "payment", "payments",
        "visa", "mastercard", "amex",
        "credit card", "debit card", "prepaid card",
        "stablecoin", "stable coin", "stable card",
        "western union", "money transfer",
        "merchant", "retailer", "point of sale",
        "remittance", "wire transfer",
        "wallet app",
        "e-commerce", "shopify",
        "cashback", "rewards", "loyalty",
    ],
    "ネットワーク健全性・技術論点": [
        "hash rate", "hashrate", "hashpower",
        "mining", "miner", "miners",
        "fork", "hard fork", "soft fork", "upgrade",
        "eip", "bip", "taproot", "segwit", "ordinals", "runes",
        "layer 2", " l2 ", "lightning", "rollup", "zk-rollup", "optimistic rollup",
        "validator", "validators", "proof of stake", "proof of work",
        "staking", "restaking",
        "vulnerability", "exploit", "hack",
        "quantum", "post-quantum",
        "protocol", "consensus",
        "throughput", "transaction fee",
        "node", "full node",
        "developer", "core dev", "bitcoin core",
        "on-chain", "onchain", "off-chain",
        "decentralization",
        "halving", "difficulty",
        "network resilience", "security budget",
    ],
    "市場心理・ポジショニング": [
        "sentiment", "fomo", "fud",
        "fear", "greed",
        "funding rate", "funding rates",
        "futures", "options", "perpetual", "perps",
        "open interest",
        "leverage", "leveraged",
        "longs", "shorts",
        "liquidation", "liquidated",
        "indicator", "indicators",
        "support level", "resistance level", "breakout", "breakdown",
        "price target",
        "bullish", "bearish",
        "rally", "pullback", "correction",
        "fatigue", "exhaustion",
        "vulnerable", "fragile",
        "volatility",
        "positioning",
        "selloff", "slide",
    ],
}

# ── サブテーマ辞書(各親テーマ内の細分カテゴリ) ──────────────────────
# 親テーマと同じ語彙を含めても良い(細分が機能すれば良い)。
SUBTHEME_KEYWORDS: dict[str, dict[str, list[str]]] = {
    "マクロ・市場環境": {
        "地政学・原油": [
            "oil", "opec", "brent", "wti", "crude", "iran", "hormuz", "war", "conflict",
            "geopolitic", "russia", "ukraine", "israel",
        ],
        "FRB・金利・インフレ": [
            "fed", "federal reserve", "fomc", "rate cut", "rate hike", "interest rate",
            "inflation", "cpi", "ppi", "recession", "gdp",
        ],
        "株式・AI": [
            "s&p", "sp500", "nasdaq", "dow", "ai slowdown", "stocks", "equities", "equity",
        ],
    },
    "政策・規制・制度": {
        "戦略備蓄・国家保有": [
            "strategic reserve", "strategic bitcoin reserve", "sovereign",
            "white house", "executive order", "president",
        ],
        "立法・議会": [
            "congress", "senate", "lawmaker", " bill ", "legislation", "framework", "policy",
        ],
        "司法・規制執行": [
            "sec", "cftc", "doj", "fbi", "prosecutor", "lawsuit", "court ruling",
            "fraud", "scam", "money laundering", "prison", "sentence", "indict",
        ],
    },
    "機関投資家・ETF・企業財務": {
        "トレジャリー会社の買い増し": [
            "microstrategy", "saylor", "strategy buys", "strategy adds", "strive",
            "smarter web", "marathon digital", "mara holdings", "bitmine",
            "treasury vehicle", "corporate treasury", "bitcoin treasury", "treasury company",
            "acquires", "buys ", "accumulate", "bought", "purchase",
        ],
        "機関リサーチ評価": [
            "bernstein", "fidelity", "vaneck", "vanguard", "blackrock", "ark invest",
            "grayscale", "td cowen", "benchmark",
            "buy rating", "sell rating", "hold rating", "initiate", "reiterate", "target price",
        ],
        "ETF フロー": [
            "etf", "etfs", "etp", "inflow", "outflow", "aum", "assets under management",
            "win streak",
        ],
    },
    "決済・実需・ユースケース": {
        "決済カード・ウォレット": [
            "visa", "mastercard", "amex", "credit card", "debit card", "prepaid card",
            "cashback", "rewards", "loyalty", "wallet app", "merchant", "retailer",
            "point of sale",
        ],
        "クロスボーダー・送金": [
            "remittance", "wire transfer", "cross-border",
        ],
    },
    "ネットワーク健全性・技術論点": {
        "マイニング・ハッシュレート": [
            "hash rate", "hashrate", "hashpower", "mining", "miner", "miners",
            "halving", "difficulty",
        ],
        "プロトコル・フォーク": [
            "fork", "hard fork", "soft fork", "upgrade",
            "eip", "bip", "taproot", "segwit", "ordinals", "runes",
            "consensus", "core dev", "bitcoin core", "developer",
        ],
        "セキュリティ・量子耐性": [
            "vulnerability", "exploit", "hack", "quantum", "post-quantum",
            "security budget", "network resilience",
        ],
        "L2・スケーリング": [
            "layer 2", " l2 ", "lightning", "rollup", "zk-rollup", "optimistic rollup",
        ],
        "ステーキング・PoS": [
            "staking", "restaking", "validator", "proof of stake",
        ],
    },
    "市場心理・ポジショニング": {
        "テクニカル・出来高": [
            "support level", "resistance level", "breakout", "breakdown",
            "thin volume", "volatility", "volume",
        ],
        "デリバティブ・先物": [
            "funding rate", "funding rates", "futures", "options", "perpetual", "perps",
            "open interest", "leverage", "leveraged", "longs", "shorts",
            "liquidation", "liquidated",
        ],
        "感情・コンセンサス": [
            "sentiment", "fomo", "fud", "fear", "greed", "bullish", "bearish",
            "fatigue", "exhaustion", "vulnerable", "fragile",
            "rally", "pullback", "correction",
        ],
    },
}

# ── 強気・対立 抽出語 ──────────────────────────────────────────────────────
BULLISH_KEYWORDS: list[str] = [
    " sees ", " says ", "leading", "signals", "expects", "positive", "optimistic",
    "bullish", "gains", "surge", "beats", "highest",
    "stabilization", "upside", "asymmetric", "win streak",
]
# 注: " rally" は「rally fatigue / vulnerable rally / stall the rally」のように
# bearish 文脈でも頻出するため BULLISH からは外している。

SKEPTICAL_KEYWORDS: list[str] = [
    " but ", "however", "warn", "concern", "lags", "falls", "fatigue",
    "vulnerable", "fragile", "bearish", "risk", "threat", "slowdown",
    "pressured", "pullback", "reverses", "slide", "selloff",
    " loses ", " drop ", "decline",
]

# 技術・中立寄りテーマの記事は 主流解釈 / 対立論点 の判定対象から外す。
# 例: MARA Foundation の量子耐性 / Sztorc の eCash フォークなど、
# 強気・弱気の市場スタンスを表す記事ではなく技術論点としての性格が強いため。
THEMES_NEUTRAL_FOR_STANCE: set[str] = {
    "ネットワーク健全性・技術論点",
    "その他",
}

# ── クラスタリング閾値 ─────────────────────────────────────────────────────
CLUSTER_JACCARD_HIGH = 0.40   # これ以上で同一イベント確定
CLUSTER_JACCARD_LOW  = 0.25   # 数値共有との合わせ技で同一イベント
LOW_COUNT_THRESHOLD     = 4   # この件数未満ならサブテーマ分割を無効
MINIMAL_COUNT_THRESHOLD = 2   # この件数未満なら 4 論点欄も省略
SUBTHEME_MIN_PER_GROUP  = 2   # サブテーマ見出しを出す最低件数

HTML_TAG_RE = re.compile(r"<[^>]+>")

# ── タイトル類似度用 ─────────────────────────────────────────────────────
STOPWORDS: set[str] = {
    "the", "and", "for", "with", "from", "into", "this", "that", "these", "those",
    "are", "was", "were", "been", "being", "have", "has", "had", "will", "would",
    "could", "should", "more", "most", "some", "any", "all", "but", "not", "you",
    "your", "his", "her", "its", "than", "what", "when", "where", "which", "while",
    "after", "before", "over", "under", "about", "onto", "upon", "via", "near",
    "says", "said", "amid", "since", "until", "off",
    "also", "may", "might", "they", "them",
    "ahead", "behind", "between", "among", "during", "without", "within",
}


# --- 読み込み -----------------------------------------------------------------


def load_matched(ticker: str) -> dict[str, Any] | None:
    path = MATCH_BASE / ticker / "matched.json"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def discover_tickers() -> list[str]:
    if not MATCH_BASE.exists():
        return []
    return sorted(
        p.name for p in MATCH_BASE.iterdir() if p.is_dir() and (p / "matched.json").exists()
    )


# --- HTML / 正規化 ------------------------------------------------------------


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    no_tags = HTML_TAG_RE.sub("", s)
    unescaped = html.unescape(no_tags)
    return re.sub(r"\s+", " ", unescaped).strip()


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        **item,
        "title": clean_text(item.get("title")),
        "summary": clean_text(item.get("summary")),
    }


# --- テーマ分類 ---------------------------------------------------------------


def classify_theme(item: dict[str, Any]) -> str:
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    scores = {theme: sum(1 for kw in kws if kw in text) for theme, kws in THEME_KEYWORDS.items()}
    max_score = max(scores.values()) if scores else 0
    if max_score == 0:
        return OTHER_THEME
    for theme in THEME_TIEBREAK_PRIORITY:
        if scores.get(theme, 0) == max_score:
            return theme
    return OTHER_THEME


def classify_subtheme(item: dict[str, Any], parent: str) -> str | None:
    """親テーマ内のサブテーマを返す。ヒットしなければ None(直下に並べる)。"""
    sub_dict = SUBTHEME_KEYWORDS.get(parent)
    if not sub_dict:
        return None
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    best_name: str | None = None
    best_score = 0
    for sub_name, kws in sub_dict.items():
        score = sum(1 for kw in kws if kw in text)
        if score > best_score:
            best_score = score
            best_name = sub_name
    return best_name


# --- 同一イベント検出 ---------------------------------------------------------

_TOK_RE = re.compile(r"[a-z0-9$,.]+")
_NUM_RE = re.compile(r"\b\d[\d,.]*\b")


def truncate_title(s: str, n: int) -> str:
    """指定文字数以下でタイトルを切る。単語境界(空白・句読点)で切って末尾に … を付ける。"""
    if not s or len(s) <= n:
        return s or ""
    cut = s[:n]
    # 後ろから区切り候補を探す。n の 60% より後ろに見つかれば単語境界で切る。
    floor = int(n * 0.6)
    best = -1
    for sep in (" ", ",", ":", ";", "—", "–", "-"):
        idx = cut.rfind(sep)
        if idx > best:
            best = idx
    if best >= floor:
        cut = cut[:best]
    return cut.rstrip(" ,.;:—–-") + "…"


def title_tokens(title: str) -> set[str]:
    """類似度比較用の小文字化トークン化(3 文字未満・ストップワード除去)。"""
    norm = title.lower().replace("'s", "").replace("’s", "")
    out: set[str] = set()
    for tok in _TOK_RE.findall(norm):
        tok = tok.strip(".,'\"")
        if len(tok) < 3 or tok in STOPWORDS:
            continue
        out.add(tok)
    return out


def numeric_tokens(title: str) -> set[str]:
    """3 桁以上の数値トークン(年号 1900-2099 を除く)を抽出。"""
    out: set[str] = set()
    for m in _NUM_RE.finditer(title):
        token = m.group().strip(".,")
        digits = re.sub(r"[^0-9]", "", token)
        if len(digits) < 3:
            continue
        if len(digits) == 4:
            try:
                if 1900 <= int(digits) <= 2099:
                    continue
            except ValueError:
                pass
        out.add(token)
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_same_event(a: dict[str, Any], b: dict[str, Any]) -> bool:
    a_tok = title_tokens(a.get("title", ""))
    b_tok = title_tokens(b.get("title", ""))
    if not a_tok or not b_tok:
        return False
    jac = jaccard(a_tok, b_tok)
    if jac >= CLUSTER_JACCARD_HIGH:
        return True
    if jac >= CLUSTER_JACCARD_LOW:
        if numeric_tokens(a.get("title", "")) & numeric_tokens(b.get("title", "")):
            return True
    return False


def cluster_items(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """同一イベントの記事を greedy にクラスタ化する。score 降順 → published_at 降順で代表選定。"""
    if not items:
        return []
    # 安定ソートで二段(pub desc → score desc。score が主、pub が副)。
    by_pub = sorted(items, key=lambda i: i.get("published_at") or "", reverse=True)
    sorted_items = sorted(by_pub, key=lambda i: i.get("score", 0), reverse=True)

    clusters: list[list[dict[str, Any]]] = []
    used: set[int] = set()
    for i, item in enumerate(sorted_items):
        if i in used:
            continue
        cluster = [item]
        used.add(i)
        for j in range(i + 1, len(sorted_items)):
            if j in used:
                continue
            if is_same_event(item, sorted_items[j]):
                cluster.append(sorted_items[j])
                used.add(j)
        clusters.append(cluster)
    return clusters


# --- 論点抽出 -----------------------------------------------------------------


def _filter_keyword_dedup(
    items: list[dict[str, Any]],
    keywords: list[str],
    url_to_cluster: dict[str, tuple[str, int]],
    url_to_theme: dict[str, str] | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """指定 keywords を含み、同一クラスターから 1 件のみ採用。タイトルヒットを要約より優先。

    url_to_theme が渡され、記事のテーマが THEMES_NEUTRAL_FOR_STANCE に入っていれば、
    技術・中立寄り記事として bullish/skeptical 抽出からスキップする。
    """
    title_hits: list[dict[str, Any]] = []
    summary_hits: list[dict[str, Any]] = []
    for item in items:
        if url_to_theme is not None:
            theme = url_to_theme.get(item.get("url", ""))
            if theme in THEMES_NEUTRAL_FOR_STANCE:
                continue
        title_text = f" {item.get('title', '')} ".lower()
        sum_text   = f" {item.get('summary', '')} ".lower()
        if any(kw in title_text for kw in keywords):
            title_hits.append(item)
        elif any(kw in sum_text for kw in keywords):
            summary_hits.append(item)

    out: list[dict[str, Any]] = []
    seen_clusters: set[tuple[str, int]] = set()
    for item in title_hits + summary_hits:
        ck = url_to_cluster.get(item.get("url", ""))
        if ck and ck in seen_clusters:
            continue
        out.append(item)
        if ck:
            seen_clusters.add(ck)
        if len(out) >= limit:
            break
    return out


def _build_url_to_cluster(
    theme_clusters: dict[str, list[list[dict[str, Any]]]],
) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for theme, clusters in theme_clusters.items():
        for idx, c in enumerate(clusters):
            for i in c:
                url = i.get("url") or ""
                if url:
                    out[url] = (theme, idx)
    return out


# --- レンダリング -------------------------------------------------------------


def render_summary_block(items: list[dict[str, Any]]) -> list[str]:
    lines = ["## サマリ(機械集計)", ""]

    pubs = [i["published_at"] for i in items if i.get("published_at")]
    if pubs:
        lines.append(f"- 期間: {min(pubs)} 〜 {max(pubs)}")
    lines.append(f"- 記事数: {len(items)}")

    source_counter = Counter(i["source_name"] for i in items)
    if source_counter:
        breakdown = ", ".join(f"{name} {n}" for name, n in source_counter.most_common())
        lines.append(f"- 媒体内訳: {breakdown}")

    term_counter: Counter[str] = Counter()
    for i in items:
        for t in i.get("matched_terms") or []:
            term_counter[t] += 1
    if term_counter:
        top_terms = ", ".join(f"{t}({n})" for t, n in term_counter.most_common(5))
        lines.append(f"- 頻出ヒット語: {top_terms}")

    recent = [i for i in items if i.get("published_at")][:3]
    if recent:
        lines.append("- 直近の見出し:")
        for i in recent:
            title = i["title"] or "(no title)"
            lines.append(f"  - {title}({i['source_name']})")

    lines.append("")
    return lines


def render_thesis_blocks(
    items: list[dict[str, Any]],
    theme_clusters: dict[str, list[list[dict[str, Any]]]],
    enable_full: bool,
) -> list[str]:
    """4 論点欄を生成する。enable_full=False(件数 <2)では主要論点のみ。"""
    lines: list[str] = []
    url_to_cluster = _build_url_to_cluster(theme_clusters)
    # url -> theme 名のマップ(技術・中立テーマを bullish/skeptical 抽出から除外する用)
    url_to_theme: dict[str, str] = {}
    for theme, clusters in theme_clusters.items():
        for c in clusters:
            for i in c:
                url = i.get("url") or ""
                if url:
                    url_to_theme[url] = theme

    # 1) 今回の主要論点
    lines += ["## 今回の主要論点", ""]
    if items:
        ordered = sorted(
            (t for t in theme_clusters if any(theme_clusters[t])),
            key=lambda t: (
                -sum(len(c) for c in theme_clusters[t]),
                THEME_DISPLAY_ORDER.index(t) if t in THEME_DISPLAY_ORDER else 99,
            ),
        )
        for theme in ordered:
            clusters = theme_clusters[theme]
            total = sum(len(c) for c in clusters)
            big = sorted([c for c in clusters if len(c) >= 2], key=len, reverse=True)[:2]
            if big:
                labels = " / ".join(
                    f"{truncate_title(c[0]['title'] or '', 55)}({len(c)}媒体)" for c in big
                )
                lines.append(f"- {theme}({total} 件) — 主要クラスター: {labels}")
            else:
                first = clusters[0][0]
                lines.append(f"- {theme}({total} 件) — 例: {truncate_title(first['title'] or '', 60)}")
    else:
        lines.append("- (記事なし)")
    lines.append("")

    if not enable_full:
        return lines

    # 2) 主流解釈(クラスター単位 de-dup、技術・中立テーマは除外)
    bullish = _filter_keyword_dedup(items, BULLISH_KEYWORDS, url_to_cluster, url_to_theme)
    lines += ["## 市場の主流解釈(強気・コンセンサスを示唆する見出し)", ""]
    if bullish:
        for it in bullish:
            lines.append(f"- ({it['source_name']}) {it['title']}")
    else:
        lines.append("- 強気の見出しは見つかりませんでした。")
    lines.append("")

    # 3) 対立論点(クラスター単位 de-dup、技術・中立テーマは除外)
    skeptical = _filter_keyword_dedup(items, SKEPTICAL_KEYWORDS, url_to_cluster, url_to_theme)
    lines += ["## 見落とされやすい対立論点(逆説・警告を示唆する見出し)", ""]
    if skeptical:
        for it in skeptical:
            lines.append(f"- ({it['source_name']}) {it['title']}")
    else:
        lines.append("- 警告/逆説的な見出しは見つかりませんでした。")
    lines.append("")

    # 4) 深掘り視点
    lines += ["## この記事で深掘りすべき視点(候補)", ""]
    minor_themes = [
        t for t in THEME_DISPLAY_ORDER
        if 1 <= sum(len(c) for c in theme_clusters.get(t, [])) <= 2
    ]
    if minor_themes:
        for t in minor_themes:
            t_items = [i for c in theme_clusters[t] for i in c]
            titles = " / ".join(i["title"] or "" for i in t_items)
            lines.append(f"- マイナーテーマ「{t}」({len(t_items)} 件): {titles}")

    bullish_urls = {i.get("url", "") for i in bullish}
    skeptical_urls = {i.get("url", "") for i in skeptical}
    unique_skeptical = [i for i in skeptical if i.get("url", "") not in bullish_urls]
    if unique_skeptical:
        lines.append("- 主流解釈に重ならない対立論点記事:")
        for i in unique_skeptical[:5]:
            lines.append(f"  - ({i['source_name']}) {i['title']}")

    # 単独高スコア(クラスター 1 件、score >= 5、主流/対立 で挙げ済みでない)
    listed = bullish_urls | skeptical_urls
    standalone: list[dict[str, Any]] = []
    for theme, clusters in theme_clusters.items():
        for c in clusters:
            if len(c) == 1 and c[0].get("score", 0) >= 5 and c[0].get("url", "") not in listed:
                standalone.append(c[0])
    if standalone:
        # スコア降順で上位 5 件
        standalone.sort(key=lambda i: i.get("score", 0), reverse=True)
        lines.append("- 単独高スコア(他媒体追随なしの独立論点):")
        for i in standalone[:5]:
            lines.append(f"  - ({i['source_name']}) [score={i.get('score', 0)}] {i['title']}")

    lines.append(
        "- 評論家視点で `config/article_style.md` の 4 観点(業界構造 / 投資家心理 / 規制 / オンチェーン需要)に接続する素材として上記を使う。"
    )
    lines.append("")
    return lines


def render_item_body(item: dict[str, Any]) -> list[str]:
    """1 記事の本文ブロック(末尾の --- は含まない。クラスター連結のため分離)。"""
    title = item["title"] or "(no title)"
    url = item["url"] or "#"
    lines = [f"### [{title}]({url})", ""]
    lines.append(f"- 媒体: {item['source_name']} (`{item['source']}`)")
    if item.get("published_at"):
        lines.append(f"- 公開: {item['published_at']}")
    if item.get("matched_terms"):
        lines.append(f"- ヒット語: {', '.join(item['matched_terms'])}")
    if "score" in item:
        lines.append(f"- Score: {item['score']}")
    lines.append("")
    if item.get("summary"):
        lines.append(item["summary"])
        lines.append("")
    return lines


def render_cluster(cluster: list[dict[str, Any]]) -> list[str]:
    """クラスター 1 つを「代表記事 + また同じイベント線」として描画する。"""
    primary = cluster[0]
    lines = render_item_body(primary)
    if len(cluster) > 1:
        also_parts: list[str] = []
        for other in cluster[1:]:
            t = (other["title"] or "")[:80]
            url = other["url"] or "#"
            also_parts.append(f"[{other['source_name']}]({url}) {t}")
        lines.append("**また同じイベント:** " + " / ".join(also_parts))
        lines.append("")
    lines.append("---")
    lines.append("")
    return lines


def build_dossier(bucket: dict[str, Any]) -> str:
    items = [normalize_item(i) for i in bucket["items"]]

    lines: list[str] = []
    lines.append(f"# {bucket['ticker']} — {bucket['name']}")
    lines.append("")
    built_at = datetime.now().astimezone().isoformat()
    lines.append(f"_Built at {built_at} from `{bucket.get('source_news_path', '?')}`_")
    lines.append("")

    lines.extend(render_summary_block(items))

    # テーマ別グルーピング
    theme_groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        theme_groups.setdefault(classify_theme(item), []).append(item)

    # 各テーマ内でクラスタリング(件数閾値で挙動を切替)
    theme_clusters: dict[str, list[list[dict[str, Any]]]] = {}
    for theme, group in theme_groups.items():
        if len(group) >= LOW_COUNT_THRESHOLD:
            theme_clusters[theme] = cluster_items(group)
        else:
            # 少件数: 各 item を 1 件単独クラスター(published_at 降順で並べる)
            sorted_grp = sorted(group, key=lambda i: i.get("published_at") or "", reverse=True)
            theme_clusters[theme] = [[i] for i in sorted_grp]

    # 4 論点欄(件数閾値で省略制御)
    enable_full = len(items) >= MINIMAL_COUNT_THRESHOLD
    lines.extend(render_thesis_blocks(items, theme_clusters, enable_full))

    # テーマ別記事一覧
    ordered_themes = [t for t in THEME_DISPLAY_ORDER if theme_clusters.get(t)]
    if theme_clusters.get(OTHER_THEME):
        ordered_themes.append(OTHER_THEME)

    for theme in ordered_themes:
        clusters = theme_clusters[theme]
        total = sum(len(c) for c in clusters)
        lines.append(f"## テーマ: {theme}({total} 件)")
        lines.append("")

        # サブテーマ展開(件数 >= LOW_COUNT_THRESHOLD かつ辞書がある場合のみ)
        if total >= LOW_COUNT_THRESHOLD and theme in SUBTHEME_KEYWORDS:
            sub_groups: dict[str | None, list[list[dict[str, Any]]]] = {}
            for c in clusters:
                sub = classify_subtheme(c[0], theme)
                sub_groups.setdefault(sub, []).append(c)

            # 表示順は SUBTHEME_KEYWORDS の登録順、未分類(None)は末尾
            sub_order: list[str | None] = list(SUBTHEME_KEYWORDS[theme].keys())
            sub_order.append(None)

            for sub in sub_order:
                cs = sub_groups.get(sub, [])
                if not cs:
                    continue
                sub_total = sum(len(c) for c in cs)
                # サブテーマ見出しは 2 件以上のときのみ(ノイズ抑制)
                if sub is not None and sub_total >= SUBTHEME_MIN_PER_GROUP:
                    lines.append(f"**サブテーマ: {sub}({sub_total} 件)**")
                    lines.append("")
                for cl in cs:
                    lines.extend(render_cluster(cl))
        else:
            for cl in clusters:
                lines.extend(render_cluster(cl))

    return "\n".join(lines)


# --- メイン -------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        tickers = [argv[1]]
    else:
        tickers = discover_tickers()

    if not tickers:
        print(
            "処理対象がありません。先に scripts/match_news.py を実行してください。",
            file=sys.stderr,
        )
        return 1

    DOSSIER_BASE.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for ticker in tickers:
        try:
            bucket = load_matched(ticker)
        except json.JSONDecodeError as e:
            print(f"[{ticker}] matched.json のパースに失敗: {e}", file=sys.stderr)
            continue

        if not bucket:
            print(f"[{ticker}] matched.json なし — スキップ", file=sys.stderr)
            continue
        if not bucket.get("items"):
            print(f"[{ticker}] items 空 — スキップ", file=sys.stderr)
            continue

        out_path = DOSSIER_BASE / f"{ticker}.md"
        try:
            out_path.write_text(build_dossier(bucket), encoding="utf-8")
        except OSError as e:
            print(f"[{ticker}] 書き出し失敗: {e}", file=sys.stderr)
            continue

        print(f"[{ticker}] {len(bucket['items'])} 記事 → {out_path.name}", file=sys.stderr)
        written.append(out_path)

    for p in written:
        print(str(p))
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

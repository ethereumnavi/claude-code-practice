#!/usr/bin/env bash
# scripts/daily_prepare.sh — 当日分の評論記事を生成する日次手動オーケストレータ。
#
# 使い方:
#   ./scripts/daily_prepare.sh
#   bash scripts/daily_prepare.sh
#
# 設計:
# - パイプラインは fetch_rss → match_news → build_dossier → write_article の 4 段。
# - 対象銘柄は config/coins.yaml の enabled: true から動的に読み込む(yaml の並び順)。
# - 前段(依存チェック / fetch_rss / match_news)は fail-fast で pipeline 停止。
# - 後段(build_dossier / write_article)は per-ticker で隔離。当日のヒットが
#   1 件以上ある銘柄だけ記事化、ヒット 0 件は SKIP として保存に進まない。
# - 進行ログ・バナー・サマリは stderr。成功した銘柄の article path のみを stdout に。
# - 各スクリプトは冪等なので、原因を直して該当ステップから手で再実行できる。

set -euo pipefail

# ── パス解決 ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ── 基本設定 ──────────────────────────────────────────────────────────────
DATE="$(date +%Y-%m-%d)"

# Python interpreter:
#   .venv/bin/python があれば優先。無ければ system の python3 にフォールバック。
PYTHON="${REPO_ROOT}/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="python3"

# ── 対象銘柄を config/coins.yaml から動的に読み込む ──────────────────────
# enabled: true のものだけ、yaml の並び順で取得する。
declare -a TICKERS=()
while IFS= read -r ticker; do
  [[ -n "$ticker" ]] && TICKERS+=("$ticker")
done < <("$PYTHON" - <<'PY'
import sys, yaml
try:
    with open("config/coins.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
except Exception as e:
    print(f"coins.yaml 読み込み失敗: {e}", file=sys.stderr)
    sys.exit(1)
for c in (data.get("coins") or []):
    if c.get("enabled", True):
        t = c.get("ticker")
        if t:
            print(t)
PY
)

if [[ "${#TICKERS[@]}" -eq 0 ]]; then
  printf '対象銘柄が 0 件です。config/coins.yaml に enabled: true の銘柄を追加してください。\n' >&2
  exit 1
fi

# ── 進捗表示ユーティリティ(すべて stderr) ─────────────────────────────
TOTAL_STEPS=$((2 + 2 * ${#TICKERS[@]}))
STEP_NUM=0
CURRENT_STEP="(init)"
SCRIPT_START=$SECONDS

# 銘柄ごとの結果を TAB 区切りで蓄積:
#   "OK\t<TICKER>\t<article_path>\t<elapsed_sec>"
#   "SKIP\t<TICKER>\tno matches\t0"
#   "FAIL\t<TICKER>\t<failed_stage>\t<elapsed_sec>"
declare -a RESULTS=()

next_step() {
  STEP_NUM=$((STEP_NUM + 1))
  CURRENT_STEP="${STEP_NUM}/${TOTAL_STEPS} $1"
  {
    printf '\n'
    printf '════════════════════════════════════════════════════════════\n'
    printf '[%s/%s] %s   (start: %s)\n' "$STEP_NUM" "$TOTAL_STEPS" "$1" "$(date +%H:%M:%S)"
    printf '════════════════════════════════════════════════════════════\n'
  } >&2
}

step_done() {
  printf '[%s/%s] %s   OK  (%ss)\n' "$STEP_NUM" "$TOTAL_STEPS" "$1" "$2" >&2
}

step_fail() {
  printf '[%s/%s] %s   FAIL  (%ss)\n' "$STEP_NUM" "$TOTAL_STEPS" "$1" "$2" >&2
}

# 前段(銘柄非依存)の失敗時のみ ERR トラップが発火する。
# 後段の per-ticker 失敗は `if` で握って RESULTS に記録するため、ここは通らない。
on_err() {
  local rc=$?
  printf '\n[FAILED] step=%s exit=%s — pipeline halted\n' "$CURRENT_STEP" "$rc" >&2
  exit "$rc"
}
trap on_err ERR

# ── 開始バナー ────────────────────────────────────────────────────────────
{
  printf 'daily_prepare\n'
  printf '  date    : %s\n' "$DATE"
  printf '  tickers : %d 銘柄(coins.yaml 由来): %s\n' "${#TICKERS[@]}" "${TICKERS[*]}"
  printf '  python  : %s\n' "$PYTHON"
} >&2

# ── 依存の事前チェック ────────────────────────────────────────────────────
CURRENT_STEP="0 deps_check"
printf '\n[deps] checking python imports...\n' >&2
"$PYTHON" - >&2 <<'PY'
import importlib, sys
required = ["yaml", "feedparser", "requests", "dateutil"]
missing = []
for m in required:
    try:
        importlib.import_module(m)
    except ImportError:
        missing.append(m)
if missing:
    print("missing modules:", ", ".join(missing), file=sys.stderr)
    print("hint: .venv を有効化するか、必要なら pip install してください。", file=sys.stderr)
    sys.exit(1)
print("ok")
PY

# ── ステップ 1: fetch_rss(銘柄非依存・fail-fast) ───────────────────────
next_step "fetch_rss.py"
t0=$SECONDS
"$PYTHON" scripts/fetch_rss.py >&2
step_done "fetch_rss.py" "$((SECONDS - t0))"

# ── ステップ 2: match_news(銘柄非依存・fail-fast) ──────────────────────
next_step "match_news.py $DATE"
t0=$SECONDS
"$PYTHON" scripts/match_news.py "$DATE" >&2
step_done "match_news.py $DATE" "$((SECONDS - t0))"

# ── 各銘柄の SKIP 判定(matched.json の items が 0 件なら SKIP) ─────────
# ELIGIBLE_TICKERS にだけ後段(build_dossier → write_article)を回す。
# SKIP した銘柄は STEP_NUM を進めず、サマリにのみ [SKIP] として残す。
declare -a ELIGIBLE_TICKERS=()
SKIP_LIST="$("$PYTHON" - "${TICKERS[@]}" <<'PY'
import json, os, sys
for t in sys.argv[1:]:
    path = os.path.join("inputs", "matches", t, "matched.json")
    items = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                items = (json.load(f).get("items") or [])
        except Exception:
            items = []
    print(f"{'OK' if items else 'SKIP'}\t{t}")
PY
)"

while IFS=$'\t' read -r kind ticker; do
  case "$kind" in
    OK)   ELIGIBLE_TICKERS+=("$ticker") ;;
    SKIP) RESULTS+=("SKIP"$'\t'"$ticker"$'\t'"no matched news"$'\t'"0") ;;
  esac
done <<< "$SKIP_LIST"

{
  printf '\n[match summary] %d 銘柄ヒット / %d 銘柄 SKIP / 全 %d 銘柄\n' \
    "${#ELIGIBLE_TICKERS[@]}" \
    "$((${#TICKERS[@]} - ${#ELIGIBLE_TICKERS[@]}))" \
    "${#TICKERS[@]}"
  if [[ "${#ELIGIBLE_TICKERS[@]}" -gt 0 ]]; then
    printf '[match summary] eligible: %s\n' "${ELIGIBLE_TICKERS[*]}"
  fi
} >&2

# ── ステップ 3〜: 銘柄ごとに dossier → article(per-ticker 隔離) ────────
for TICKER in "${ELIGIBLE_TICKERS[@]}"; do
  TICKER_START=$SECONDS

  # build_dossier
  next_step "build_dossier.py $TICKER"
  t0=$SECONDS
  if "$PYTHON" scripts/build_dossier.py "$TICKER" >&2; then
    step_done "build_dossier.py $TICKER" "$((SECONDS - t0))"
  else
    step_fail "build_dossier.py $TICKER" "$((SECONDS - t0))"
    RESULTS+=("FAIL"$'\t'"$TICKER"$'\t'"build_dossier"$'\t'"$((SECONDS - TICKER_START))")
    continue
  fi

  # write_article(成功時 stdout に article path を返す)
  next_step "write_article.sh $TICKER $DATE"
  t0=$SECONDS
  if ARTICLE_PATH="$(bash scripts/write_article.sh "$TICKER" "$DATE")"; then
    step_done "write_article.sh $TICKER $DATE" "$((SECONDS - t0))"
    RESULTS+=("OK"$'\t'"$TICKER"$'\t'"$ARTICLE_PATH"$'\t'"$((SECONDS - TICKER_START))")
  else
    step_fail "write_article.sh $TICKER $DATE" "$((SECONDS - t0))"
    RESULTS+=("FAIL"$'\t'"$TICKER"$'\t'"write_article"$'\t'"$((SECONDS - TICKER_START))")
  fi
done

# ── 完了サマリ(stderr) ──────────────────────────────────────────────────
TOTAL_ELAPSED=$((SECONDS - SCRIPT_START))
TOTAL_MIN=$((TOTAL_ELAPSED / 60))
TOTAL_SEC=$((TOTAL_ELAPSED % 60))

OK_COUNT=0
SKIP_COUNT=0
FAIL_COUNT=0
for entry in "${RESULTS[@]}"; do
  case "${entry%%$'\t'*}" in
    OK)   OK_COUNT=$((OK_COUNT + 1)) ;;
    SKIP) SKIP_COUNT=$((SKIP_COUNT + 1)) ;;
    FAIL) FAIL_COUNT=$((FAIL_COUNT + 1)) ;;
  esac
done

{
  printf '\n─────────────────────────────────────────────\n'
  printf 'daily_prepare done\n'
  printf '  date    : %s\n' "$DATE"
  printf '  results : OK=%d / SKIP=%d / FAIL=%d / total=%d\n' \
    "$OK_COUNT" "$SKIP_COUNT" "$FAIL_COUNT" "${#TICKERS[@]}"
  # TICKERS の yaml 順で OK / SKIP / FAIL を一覧表示する。
  for TICKER in "${TICKERS[@]}"; do
    for entry in "${RESULTS[@]}"; do
      IFS=$'\t' read -r status t detail elapsed <<< "$entry"
      if [[ "$t" == "$TICKER" ]]; then
        case "$status" in
          OK)
            printf '    [OK]   %-6s  %s  (%ss)\n' "$t" "$detail" "$elapsed"
            ;;
          SKIP)
            printf '    [SKIP] %-6s  %s\n' "$t" "$detail"
            ;;
          FAIL)
            printf '    [FAIL] %-6s  %s に失敗  (%ss)\n' "$t" "$detail" "$elapsed"
            ;;
        esac
        break
      fi
    done
  done
  printf '  total   : %ss(約%d分%d秒)\n' "$TOTAL_ELAPSED" "$TOTAL_MIN" "$TOTAL_SEC"
  printf '─────────────────────────────────────────────\n'
} >&2

# ── stdout: 成功した銘柄の article path だけを TICKERS の yaml 順に 1 行ずつ ──
for TICKER in "${TICKERS[@]}"; do
  for entry in "${RESULTS[@]}"; do
    IFS=$'\t' read -r status t detail _elapsed <<< "$entry"
    if [[ "$t" == "$TICKER" && "$status" == "OK" ]]; then
      printf '%s\n' "$detail"
      break
    fi
  done
done

# 後段の per-ticker 失敗・SKIP は exit 0 のまま(サマリで報告済み)。
# 前段失敗時は trap on_err で先に exit 1 されるため、ここには到達しない。

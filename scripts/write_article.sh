#!/usr/bin/env bash
# scripts/write_article.sh — TICKER の dossier から評論記事を生成して保存する。
#
# 使い方:
#   bash scripts/write_article.sh BTC [YYYY-MM-DD]
#
# 設計:
# - 一時ファイルを最終出力ディレクトリに作り、検証通過時だけ mv で昇格(原子的)
# - 失敗時は tmp を必ず削除し、本番ファイルには空ファイルを残さない
# - 成功時のみ stdout に保存先パスを出力する(後段で xargs cat $(...) できる)
# - 進捗・エラーは stderr に出すと同時に logs/ に永続保存

set -euo pipefail

usage() {
  cat <<'EOF' >&2
Usage: bash scripts/write_article.sh <TICKER> [YYYY-MM-DD]

  TICKER     対象銘柄(例: BTC)。inputs/dossiers/<TICKER>.md が必要。
  YYYY-MM-DD 出力ファイル名に使う日付。省略時は today。
EOF
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 1
fi

TICKER="$1"
DATE="${2:-$(date +%Y-%m-%d)}"

# ── パス解決 ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DOSSIER="$REPO_ROOT/inputs/dossiers/$TICKER.md"
STYLE="$REPO_ROOT/config/article_style.md"
PROMPT_TEMPLATE="$REPO_ROOT/prompts/write_article_prompt.md"
RESEARCH="$REPO_ROOT/inputs/research/$TICKER.md"   # 任意。存在すれば prompt に注入
OUT_DIR="$REPO_ROOT/outputs/articles"
LOG_DIR="$REPO_ROOT/logs"

TICKER_LOWER="$(echo "$TICKER" | tr '[:upper:]' '[:lower:]')"
FINAL_PATH="$OUT_DIR/${DATE}-${TICKER_LOWER}.md"

# tmp は最終出力ディレクトリと同じ FS に置く(同一 FS なので mv が原子的)
BODY_TMP="$OUT_DIR/.${DATE}-${TICKER_LOWER}.body.tmp"
PROMPT_TMP="$OUT_DIR/.${DATE}-${TICKER_LOWER}.prompt.tmp"

# ── ログ準備 ──────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR" "$OUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_PATH="$LOG_DIR/write_article-${TS}-${TICKER_LOWER}.log"

log() {
  printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG_PATH" >&2
}

cleanup() {
  rm -f "$BODY_TMP" "$PROMPT_TMP"
}
# 通常終了・異常終了どちらでも tmp を掃除する。
# 成功時は BODY_TMP は mv 済みなので rm -f は no-op、PROMPT_TMP だけ消える。
trap cleanup EXIT

log "起動: TICKER=$TICKER DATE=$DATE"
log "ログ: $LOG_PATH"

# ── 前提チェック ──────────────────────────────────────────────────────────
fail() {
  log "FAIL: $*"
  exit 1
}

command -v claude >/dev/null 2>&1 \
  || fail "claude コマンドが PATH にありません。Claude Code をインストールしてください。"
[[ -f "$DOSSIER"         ]] || fail "dossier が見つかりません: $DOSSIER"
[[ -f "$STYLE"           ]] || fail "style が見つかりません: $STYLE"
[[ -f "$PROMPT_TEMPLATE" ]] || fail "プロンプトテンプレが見つかりません: $PROMPT_TEMPLATE"

# ── プロンプト組み立て ────────────────────────────────────────────────────
log "プロンプト組み立て中..."
if [[ -f "$RESEARCH" ]]; then
  log "HASHHUB CONTEXT を使用: $RESEARCH"
else
  log "HASHHUB CONTEXT は未配置: スキップ($RESEARCH)"
fi
{
  cat "$PROMPT_TEMPLATE"
  printf '\n\n===== DOSSIER (TICKER=%s, DATE=%s) =====\n\n' "$TICKER" "$DATE"
  cat "$DOSSIER"
  if [[ -f "$RESEARCH" ]]; then
    printf '\n\n===== HASHHUB CONTEXT (TICKER=%s) =====\n\n' "$TICKER"
    cat "$RESEARCH"
  fi
  printf '\n\n===== STYLE =====\n\n'
  cat "$STYLE"
  printf '\n\n===== END =====\n\n本文だけを出力してください。前置き・解説・出力全体のコードフェンスは禁止。\n'
} > "$PROMPT_TMP"

PROMPT_BYTES=$(wc -c < "$PROMPT_TMP" | tr -d ' ')
log "プロンプトサイズ: ${PROMPT_BYTES} bytes"

# ── Claude 実行(非対話)──────────────────────────────────────────────────
# --allowedTools "" で全ツールを無効化し、純テキスト出力に閉じ込める。
# CLI バージョンによってフラグ名が `--allowed-tools` の場合があります。
# 通らない場合は `claude -h` を確認のうえ下記の行を調整してください。
log "claude を非対話モードで実行中(ツール無効化)..."
CLAUDE_EXIT=0
claude -p "$(cat "$PROMPT_TMP")" --allowedTools "" > "$BODY_TMP" 2>>"$LOG_PATH" || CLAUDE_EXIT=$?

if (( CLAUDE_EXIT != 0 )); then
  fail "claude 実行に失敗(exit $CLAUDE_EXIT)。詳細は $LOG_PATH"
fi

# ── 出力検証 ───────────────────────────────────────────────────────────────
if [[ ! -s "$BODY_TMP" ]]; then
  fail "出力が空です。"
fi

BODY_BYTES=$(wc -c < "$BODY_TMP" | tr -d ' ')
log "出力サイズ: ${BODY_BYTES} bytes"

if (( BODY_BYTES < 1500 )); then
  log "出力先頭(先頭 200 バイトをログに記録):"
  head -c 200 "$BODY_TMP" >> "$LOG_PATH" 2>/dev/null || true
  printf '\n' >> "$LOG_PATH"
  fail "出力が短すぎます(${BODY_BYTES} bytes)。約 5,000 字の記事には不足。"
fi

# ── Post-process: 半角括弧の出典を全角に矯正 ──────────────────────────────
# `(タイトル - メディア)` のように、半角括弧で囲まれ、内部に ` - `(半角ハイフン
# 両側スペース)を含むものだけを `（タイトル - メディア）` に置換する。
# URL や Markdown 記法、見出し・本文中の単純な `()` は対象外(` - ` を含まないため)。
# 以降の検証はすべて、この置換 *後* のファイル内容に対して行う。
if command -v python3 >/dev/null 2>&1; then
  PP_RESULT="$(python3 - "$BODY_TMP" <<'PY' 2>&1
import re, sys
path = sys.argv[1]
with open(path, encoding='utf-8') as f:
    text = f.read()
# 半角括弧で囲まれ、内側に ` - ` を持つもののみ全角化。改行・括弧の入れ子は除外。
new_text, count = re.subn(r'\(([^()\n]+ - [^()\n]+)\)', r'（\1）', text)
if count:
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_text)
print(f"converted {count}")
PY
)"
  log "post-process 半角→全角括弧: ${PP_RESULT}"
else
  log "WARN: python3 が見つかりません。post-process をスキップします。"
fi

FIRST_LINE="$(head -n 1 "$BODY_TMP")"
if [[ -z "$FIRST_LINE" ]]; then
  fail "本文の 1 行目が空です(タイトル行が欠落)。"
fi
if [[ "$FIRST_LINE" == \#* ]]; then
  log "1 行目: $FIRST_LINE"
  fail "1 行目はタイトルのプレーンテキストにしてください('#' で始めない)。"
fi

# 必須見出し(レベル 1)。`## 前提` 等の旧形式は弾く。
for marker in '# 前提' '# 総括' '# 参考文献'; do
  if ! grep -qE "^${marker}[[:space:]]*$" "$BODY_TMP"; then
    fail "必須見出しが欠落しています(レベル 1 で書いてください): $marker"
  fi
done

# ── HASHHUB CONTEXT 由来の出典化禁止(fail) ─────────────────────────────
# プロンプトで明示的に禁止しているとおり、本文・参考文献のいずれにおいても
# `（… - HashHub Research）` のような形で HashHub Research を出典化することは不可。
# 半角・全角どちらの括弧も検出対象。`HashHub Research` は大文字小文字を無視。
HASHHUB_HITS="$(grep -niE '[（(][^（）()]*HashHub Research[^（）()]*[）)]' "$BODY_TMP" || true)"
if [[ -n "$HASHHUB_HITS" ]]; then
  log "出力に HashHub Research を出典として含む箇所を検出:"
  while IFS= read -r line; do
    [[ -n "$line" ]] && log "  $line"
  done <<< "$HASHHUB_HITS"
  fail "HASHHUB CONTEXT を出典化することは禁止です(本文・参考文献いずれも不可)。"
fi

# ── インライン出典の検証(警告のみ、保存は阻害しない)──────────────────
# 想定書式: 句点直前に全角括弧で「（記事タイトル - メディア名）」。
# 「# 参考文献」より前(本文)に出てくる該当パターンを数える。
BODY_ONLY=$(awk '/^# 参考文献[[:space:]]*$/{exit} {print}' "$BODY_TMP")
# pipefail 下で grep の 0 件マッチ(exit 1)が pipeline を殺さないよう || true で吸収。
INLINE_COUNT=$( { printf '%s' "$BODY_ONLY" | grep -oE '（[^（）]+ - [^（）]+）' || true; } | wc -l | tr -d ' ')
log "インライン出典: ${INLINE_COUNT} 個"

if (( INLINE_COUNT < 3 )); then
  log "WARN: インライン出典が ${INLINE_COUNT} 個しかありません(目安は 3 個以上)。プロンプトの「本文中の出典」が反映されているか確認してください。"
fi

# ── 桁ずれ警告(billion / million / trillion → 億ドル の誤変換検知)──────
# dossier に登場する `$N billion|million|trillion` の N が、記事本文に
# 同じ N で `N億ドル` として現れていたら桁崩れの可能性として WARN。
# 限界:
#  - dossier に出ない数値や、N が偶然一致した別文脈の数値は検知できない。
#  - 記事側の表現が「億ドル」以外(例: 万ドル、兆ドル、英語のまま)なら対象外。
#  - 検出は弱いシグナルなので fail にせず WARN のみで人間判断に委ねる。
if command -v python3 >/dev/null 2>&1; then
  MAGNITUDE_RESULT="$(python3 - "$DOSSIER" "$BODY_TMP" <<'PY' 2>&1
import re, sys
dossier = open(sys.argv[1], encoding='utf-8').read()
article = open(sys.argv[2], encoding='utf-8').read()

def find_amounts(text, unit):
    pattern = rf'\$\s*([\d,]+(?:\.\d+)?)\s*{unit}\b'
    return sorted({m.group(1) for m in re.finditer(pattern, text, re.IGNORECASE)})

def fmt_oku(value):
    if value == int(value):
        return f"{int(value):,}億ドル"
    return f"{value:g}億ドル"

def fmt_man(value):
    if value == int(value):
        return f"{int(value):,}万ドル"
    return f"{value:g}万ドル"

def fmt_cho(value):
    if value == int(value):
        return f"{int(value):,}兆ドル"
    return f"{value:g}兆ドル"

warnings = []

# billion: 期待値は (N×10)億ドル。記事に N億ドル があれば桁ずれ疑い。
for n_str in find_amounts(dossier, 'billion'):
    try:
        n = float(n_str.replace(',', ''))
    except ValueError:
        continue
    if re.search(re.escape(n_str) + r'億ドル', article):
        warnings.append(f"  - dossier: ${n_str} billion / 期待: {fmt_oku(n*10)} / 記事: {n_str}億ドル")

# million: 期待値は N×100万ドル(= N/100億ドル)。記事に N億ドル があれば 100倍ずれ疑い。
for n_str in find_amounts(dossier, 'million'):
    try:
        n = float(n_str.replace(',', ''))
    except ValueError:
        continue
    if re.search(re.escape(n_str) + r'億ドル', article):
        warnings.append(f"  - dossier: ${n_str} million / 期待: {fmt_man(n*100)} / 記事: {n_str}億ドル")

# trillion: 期待値は N兆ドル。記事に N億ドル があれば 1/10000ずれ疑い。
for n_str in find_amounts(dossier, 'trillion'):
    try:
        n = float(n_str.replace(',', ''))
    except ValueError:
        continue
    if re.search(re.escape(n_str) + r'億ドル', article):
        warnings.append(f"  - dossier: ${n_str} trillion / 期待: {fmt_cho(n)} / 記事: {n_str}億ドル")

if warnings:
    print("WARN: 桁ずれの疑い(billion/million/trillion → 億ドル変換):")
    for w in warnings:
        print(w)
else:
    print("ok")
PY
)"
  while IFS= read -r line; do
    [[ -n "$line" ]] && log "$line"
  done <<< "$MAGNITUDE_RESULT"
else
  log "WARN: python3 が見つかりません。桁ずれ検証をスキップします。"
fi

log "検証 OK"

# ── 昇格(原子的)──────────────────────────────────────────────────────────
mv "$BODY_TMP" "$FINAL_PATH"
log "保存: $FINAL_PATH"

# 成功時のみ stdout に最終パス
echo "$FINAL_PATH"

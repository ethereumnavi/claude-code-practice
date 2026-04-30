# claude-code-practice

Claude Code のハンズオン用リポジトリ。
暗号資産ニュースの RSS を集め、銘柄ごとに dossier を作り、評論家トーンの日本語記事(約 5,000 字)を Markdown で出力するパイプラインを最小構成で組む。

## ディレクトリ構成

```
.
├── README.md
├── CLAUDE.md                     # 将来のセッション向けリポジトリ規約
├── config/
│   ├── rss_sources.yaml          # 取得対象の RSS フィード一覧
│   ├── coins.yaml                # 監視対象の銘柄(別名・関連語つき)
│   └── article_style.md          # 記事の文体・構成ルール(LLM の system プロンプトに流す)
├── inputs/
│   ├── raw/                      # fetch_rss.py の生出力(RSS エントリの正規化済み JSON)
│   ├── matches/                  # 銘柄マッチ結果の中間ファイル
│   └── dossiers/                 # 銘柄ごとに集約した dossier(JSON)
├── outputs/
│   ├── articles/                 # 生成された Markdown 記事
│   └── image-prompts/            # (任意)画像生成用プロンプト
└── scripts/                      # 実行スクリプト置き場(中身は未実装)
```

## パイプライン(予定)

```
RSS feeds
   │  fetch_rss.py
   ▼
inputs/raw/*.json
   │  match_news.py        # config/coins.yaml でフィルタ
   ▼
inputs/matches/*.json
   │  build_dossier.py     # 銘柄ごとに集約
   ▼
inputs/dossiers/<TICKER>.json
   │  write_article.py     # config/article_style.md を system プロンプトに使用
   ▼
outputs/articles/<TICKER>.md
   └─(任意)─▶ outputs/image-prompts/<TICKER>.txt
```

各段は独立したスクリプトとして実装し、中間成果物をディスクに残すことで、
失敗した段だけリトライできるようにする方針。

## 現在の状態

- ディレクトリ構成と設定ファイルの雛形のみ作成済み。
- `config/rss_sources.yaml` の URL は **TODO** プレースホルダ。
  ハンズオン本番前に各メディアの RSS URL を確認して埋めること。
- `scripts/` 配下のスクリプト本体は未着手。

## 日次の手動実行

毎朝この 1 コマンドで、ETH / SOL / BTC の評論記事 3 本が `outputs/articles/<date>-<ticker>.md` に生成されます(配列順に処理されるため、出力順・確認順は ETH → SOL → BTC)。

```bash
./scripts/daily_prepare.sh
```

`bash scripts/daily_prepare.sh` でも同じです。所要時間の目安は 3 銘柄合計で 15〜25 分(LLM 呼び出しが銘柄ごとに 5〜7 分)。

### 毎朝叩く 3 パターン(コピペ用)

生成だけ:

```bash
./scripts/daily_prepare.sh
```

VS Code で全部開く(成功した銘柄分):

```bash
code $(./scripts/daily_prepare.sh)
```

主役の 1 本目(ETH)だけ macOS の規定アプリで開く:

```bash
./scripts/daily_prepare.sh | head -1 | xargs open
```

### 中で動くこと

`scripts/daily_prepare.sh` は次の 4 段を呼び出します:

1. `python scripts/fetch_rss.py` — RSS を取得 → `inputs/raw/<date>/news.json`
2. `python scripts/match_news.py <date>` — 銘柄でフィルタ → `inputs/matches/<TICKER>/matched.json`
3〜4. 各銘柄(ETH → SOL → BTC の順)で:
   - `python scripts/build_dossier.py <TICKER>` — 集約 → `inputs/dossiers/<TICKER>.md`
   - `bash scripts/write_article.sh <TICKER> <date>` — 記事生成 → `outputs/articles/<date>-<ticker>.md`

`<date>` はスクリプト冒頭で `date +%Y-%m-%d` を 1 度だけ採取し、すべての段で同じ値を使います。`.venv/bin/python` があれば優先し、無ければ system の `python3` にフォールバックします。

### fail-fast / per-ticker 隔離の切り分け

- **前段(依存チェック / fetch_rss / match_news)** は fail-fast。落ちたら pipeline 全体停止、`[FAILED] step=N/M ... exit=...` を stderr に出して exit 1。
- **後段(build_dossier / write_article)** は per-ticker で隔離。ETH が落ちても SOL/BTC は続行します。最後のサマリで OK / FAIL を一覧表示。daily_prepare 全体の exit code は前段が成功していれば 0(後段の per-ticker 失敗はサマリ報告のみ)。

### 出力の流れ

- **stdout**: 成功した銘柄の article path を **配列順に 1 行ずつ**(ETH → SOL → BTC)。失敗した銘柄の path は出さない
- **stderr**: バナー、各ステップの進行ログ、銘柄別 OK/FAIL サマリ、総時間

### 失敗したらどこから再開するか

各段は前段の出力ファイルだけを入力に取る独立したスクリプトです。失敗した銘柄だけを手で叩き直せます。

例えば ETH の write_article で落ちた場合:

```bash
bash scripts/write_article.sh ETH $(date +%Y-%m-%d)
```

build_dossier から再走したい場合(matched.json が当日分残っている前提):

```bash
.venv/bin/python scripts/build_dossier.py ETH
bash scripts/write_article.sh ETH $(date +%Y-%m-%d)
```

`scripts/write_article.sh` 単体の使い方や検証ルールは下記「記事生成」節を参照してください。

### 銘柄を増やしたい・順序を変えたいとき

[scripts/daily_prepare.sh](scripts/daily_prepare.sh) の `TICKERS=("ETH" "SOL" "BTC")` 配列を編集するだけです。コードの他の場所には手を入れません。新しい銘柄を増やす場合は、事前に [config/coins.yaml](config/coins.yaml) にエントリを追加してから `TICKERS` に入れてください。

[inputs/research/](inputs/research/) に `<TICKER>.md` を置くと per-ticker で背景知識が prompt に注入されます(opt-in、無くても動く)。雛形は `inputs/research/BTC.md.example` / `ETH.md.example` / `SOL.md.example`。詳しくは下記「記事生成」節 → 「現行仕様」 → 「背景知識」サブ節を参照してください。

## 記事生成

dossier から評論記事(約 5,000 字、Markdown)を生成して `outputs/articles/` に保存します。本文生成は Claude Code(`claude`)を非対話モードで呼び出し、`scripts/write_article.sh` が前後処理(プロンプト組み立て・出力検証・原子的保存・ログ記録)を担当します。

### 事前条件

- Python 3 と pip が利用可能であること(`python3 --version` で確認)。
- Claude Code(`claude`)がインストール済みで認証済みであること。`claude -h` で起動を確認してください。
- 対象銘柄の dossier(`inputs/dossiers/<TICKER>.md`)が存在すること。先行する `fetch_rss.py` → `match_news.py` → `build_dossier.py` を実行済みである必要があります。
- `config/article_style.md` と `prompts/write_article_prompt.md` が存在すること(リポジトリに同梱)。

### 仮想環境の有効化

初回のみ:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2 回目以降は有効化のみで OK:

```bash
source .venv/bin/activate
```

### 実行例

today の日付で BTC の記事を生成する場合:

```bash
bash scripts/write_article.sh BTC
```

日付を明示する場合(出力ファイル名に使われます):

```bash
bash scripts/write_article.sh BTC 2026-04-29
```

進捗ログは stderr と `logs/write_article-<timestamp>-<ticker>.log` に出力されます。**成功時のみ** stdout に保存先パスが 1 行返るので、後段で `xargs` 等に渡せます。

### 出力先

- 記事本文: `outputs/articles/YYYY-MM-DD-<ticker>.md`(ticker は小文字化されます)
- 実行ログ: `logs/write_article-YYYYMMDD-HHMMSS-<ticker>.log`(成否を問わず残ります)

検証を通過したファイルだけが本番パスに昇格します。失敗時は一時ファイルが自動削除され、本番パスには空ファイルや不完全ファイルが残りません。検証の中身は下記「現行仕様」を参照してください。

### 現行仕様

`scripts/write_article.sh` が生成・検証する記事の書式を、現行版として以下に固定します。詳細な指示文は [prompts/write_article_prompt.md](prompts/write_article_prompt.md) を、検証ロジックは [scripts/write_article.sh](scripts/write_article.sh) を参照してください。

#### タイトル・見出し

- 1 行目は記事タイトルのプレーンテキスト 1 行(`#` 等の見出し記号を付けない)
- 本文セクションはすべて見出しレベル 1 で書く: `# 前提` / `# <銘柄>を取り巻く業界構造` / `# 投資家心理の温度感` / `# 規制をめぐる現在地` / `# オンチェーン需要から見える実需` / `# 総括` / `# 参考文献`。順番固定、`## ` 以下の小見出しは使わない

#### 本文中の出典(全角括弧)

- 数値・発言・単一ソース由来の観察を扱う文には、句点 `。` の直前に全角括弧で `（記事タイトル - メディア名）` を入れる
- URL は本文に出さない(末尾の `# 参考文献` にのみ列挙)
- 半角 `(` `)` は出力に出さない。`scripts/write_article.sh` 内の post-process が保険として半角→全角に矯正する

#### 重要文の太字強調

- 各セクションで「論点・要点・主張・結論」にあたる 1〜2 文を `**…**` で太字にする
- とくに `# 前提` と `# 総括` には、**それだけ読んでも記事全体の主張が掴める太字文を 1 文以上**入れる

#### 参考文献(リスト形式)

- 各行は `- （記事タイトル - メディア名） URL`。コードブロックで囲まない。`[…](URL)` のリンク化もしない
- 並びは本文での初出順(公開日時順・アルファベット順ではない)
- URL の tracking parameter(`?utm_source=...` 等)は除去
- 英語タイトルはそのまま保持(翻訳しない)

#### 数値・通貨単位の桁変換

`million=100万 / billion=10億 / trillion=1兆` の対応表に厳密に従う。

| 元 | 変換後 |
|---|---|
| `$60 million` | `6,000万ドル`(`0.6億ドル` は使わない) |
| `$255 million` | `2.55億ドル` |
| `$2.55 billion` | `25.5億ドル` |
| `$63.7 billion` | `637億ドル` |
| `$2 billion` | `20億ドル`(`20.0億ドル` のように小数 0 を残さない) |
| `$1 trillion` | `1兆ドル` |

数値の係数部(`63.7`、`818,334` 等)は変えない。不確実なときは英語表記のまま残す。参考文献の英語タイトル内の `$63.7 billion` 等は変換せずそのまま保持する(変換は本文の日本語叙述にだけ適用)。

#### 検証ルール(fail と WARN の切り分け)

`scripts/write_article.sh` の検証は post-process(半角→全角括弧)後の最終出力に対して実行されます。

| 項目 | レベル | 条件 |
|---|---|---|
| 出力空・極端に短い(< 1500 bytes) | fail | claude が本文を返していない可能性 |
| 1 行目が空 / `#` で始まる | fail | タイトル行が欠落・見出し化されている |
| `# 前提` / `# 総括` / `# 参考文献` が行頭に存在しない | fail | 必須セクション欠落(`## 前提` のような旧書式も失格) |
| 本文・参考文献に `HashHub Research` が括弧で登場 | fail | HASHHUB CONTEXT は出典化禁止(下記「背景知識」サブ節を参照) |
| インライン出典が 3 個未満 | WARN | プロンプトの出典指示が反映されていない疑い |
| 桁ずれ疑い(`$N billion` と `N億ドル` が dossier・記事に同居 等) | WARN | 元桁を壊した変換の可能性。dossier との照合で検出 |

WARN は人間判断に委ねる方針(保存は阻害しない)。検出は弱いシグナルなので、検証側の限界(false positive / false negative の可能性)は `scripts/write_article.sh` 内のコメントを参照してください。

#### 背景知識(HASHHUB CONTEXT、任意)

`inputs/research/<TICKER>.md` を配置すると、`scripts/write_article.sh` が prompt 末尾に `===== HASHHUB CONTEXT =====` ブロックとして注入します。ファイルが無ければ何もしない opt-in 仕様で、現行仕様の動作には影響しません。

- **位置づけ**: 「枠組み・視点・長期論点」を補助する材料(stock 知識)。日次ニュース dossier(flow 知識)とは別扱い
- **入れて良い**: 時間に依らない論点、業界構造の枠組み、規制の枠組み解説、オンチェーン需要の読み方、マクロの構造論
- **入れてはいけない**: 特定の日付・価格・数値、過去の予測・ターゲット、固有のアクション履歴、競合銘柄の長期論
- **本文での扱い**: 評論家自身の背景知識として自然に溶かす。「HashHub Research では〜」のような attribution は禁止。HASHHUB CONTEXT 由来の数値・日付・固有の出来事を本文のファクトとして起こさない(事実は dossier から取る)
- **出典扱い**: HASHHUB CONTEXT は本文の `（… - …）` インライン出典にも `# 参考文献` リストにも一切登場させない(検出時 fail)
- **配置とコミット**: `inputs/research/*.md` は `.gitignore` 対象。雛形 `inputs/research/BTC.md.example` のみコミット可
- **last reviewed の運用**: ファイル冒頭に `last reviewed: YYYY-MM-DD` を残す運用を**推奨**(必須ではない)。コンテンツ更新時に上書き
- **存在しないとき**: ブロックは挿入されず、現行仕様どおりに動作する

詳細な指示文は [prompts/write_article_prompt.md](prompts/write_article_prompt.md) の「背景知識(HASHHUB CONTEXT)の扱い」セクションを、雛形は [inputs/research/BTC.md.example](inputs/research/BTC.md.example) を参照してください。

### よくある失敗例

- **`claude コマンドが PATH にありません`**: Claude Code の CLI(`claude`)がインストールされていない、もしくは認証されていません。`which claude` と `claude -h` で確認してください。
- **`dossier が見つかりません: inputs/dossiers/<TICKER>.md`**: 上流パイプラインが未実行か、その銘柄でヒットがありませんでした。`python scripts/fetch_rss.py && python scripts/match_news.py && python scripts/build_dossier.py` を先に流してください。
- **`unknown option: --allowedTools` 系のエラー**: `claude` CLI のバージョンによってフラグ名が `--allowed-tools`(kebab-case)の場合があります。`claude -h` で確認のうえ `scripts/write_article.sh` の該当行を調整してください。
- **`出力が短すぎます` / `必須見出しが欠落しています`**: モデルが本文以外(拒否文・前置き・出力全体をコードフェンスでラップした応答など)を返した可能性があります。`logs/` に出力先頭 200 バイトが記録されているので確認のうえ、もう一度実行するか、`prompts/write_article_prompt.md` を見直してください。
- **`ModuleNotFoundError` 等**: 仮想環境を有効化しているか(`which python` が `.venv` 配下を指しているか)、`pip install -r requirements.txt` を実行済みかを確認してください。

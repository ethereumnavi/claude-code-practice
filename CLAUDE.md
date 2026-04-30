# リポジトリ規約(Claude 向け)

このファイルは、将来のセッションで Claude が文脈を素早く立ち上げるためのメモ。
人間の運用ドキュメントは README.md を参照すること。

## 何のリポジトリか

暗号資産ニュースの RSS を集めて評論家トーンの日本語記事を生成する、ハンズオン用の最小構成パイプライン。
プロダクション運用は想定していない。複雑化させない。

## パイプラインの段(必ずこの順序で1方向)

1. `scripts/fetch_rss.py`     — `config/rss_sources.yaml` を読み、`inputs/raw/` に正規化 JSON を書く。
2. `scripts/match_news.py`    — `config/coins.yaml` を使って RSS エントリを銘柄でフィルタし、`inputs/matches/` に書く。
3. `scripts/build_dossier.py` — 銘柄ごとに集約し `inputs/dossiers/<TICKER>.json` を作る。
4. `scripts/write_article.py` — `config/article_style.md` を system プロンプトに使い、`outputs/articles/<TICKER>.md` を生成。
   余力があれば `outputs/image-prompts/<TICKER>.txt` も出す。

各段は独立。前段の出力ファイルだけを入力として読む。段をまたぐデータをメモリで持ち回さない。

## ライブラリ方針

- 標準ライブラリ + `feedparser` / `pyyaml` / `python-dateutil` 程度に留める。
- LLM 呼び出しは Anthropic SDK(`anthropic`)のみ。既定モデルは Claude Sonnet 4.6。
- 重い依存(pandas, scrapy, langchain など)は入れない。

## 設定ファイルの扱い

- `config/rss_sources.yaml` と `config/coins.yaml` はユーザーが手で編集する想定。
  スクリプトから書き換えない。
- `config/article_style.md` は LLM の system プロンプトとしてそのまま流し込む。
  「LLM への指示書」として読める日本語で書く。

## 記事生成のルール

- 文体・構成・禁止事項は `config/article_style.md` が一次情報。ここを更新するときは衝突する古い指示を残さない。
- 評論家ペルソナ。価格予想を断定しない。素材にない数値は書かない。
- 約 5,000 字(±10%)を目安。短ければ観点ごとの事実補足で調整、水増し表現で稼がない。

## やらないこと

- 各メディアの本文を全文スクレイピングする処理は入れない(RSS の summary までで運用する)。
- 銘柄マッチの ML 化、ベクタ検索化、DB 化はしない。YAML + 正規表現で十分。
- 中間成果物のフォーマット(JSON のキー名)を理由なく変えない。段間の互換が壊れる。

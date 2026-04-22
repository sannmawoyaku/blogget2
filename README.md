# blogget2

日向坂46公式ブログの新着を取得し、Slackへ通知するスクリプトです。
要約には Gemini API を使用します。

## 環境変数

- `SLACK_WEBHOOK_URL`: Slack Incoming Webhook URL
- `GEMINI_API_KEY`: Gemini APIキー
- `GEMINI_MODEL` (任意): 使用するGeminiモデル名。デフォルト `gemini-2.5-flash`
- `GEMINI_DAILY_REQUEST_LIMIT` (任意): 1日あたりのGemini呼び出し上限。デフォルト `200`
- `MAX_ARTICLES_PER_RUN` (任意): 1回の実行で処理する記事の最大件数。デフォルト `200`
- `TARGET_DAYS_AGO` (任意): 何日前の記事を対象にするか。デフォルト `1`（前日分）
- `GEMINI_MIN_INTERVAL_SECONDS` (任意): Gemini呼び出しの最小間隔（秒）。デフォルト `5`
- `GEMINI_USAGE_HISTORY_DAYS` (任意): 利用カウント履歴を保持する日数。デフォルト `120`
- `BLOG_SOURCES` (任意): 対象サイト。カンマ区切りで指定。デフォルト `hinatazaka,nogizaka`

## 無料枠を超えにくくするための挙動

- 対象日は `TARGET_DAYS_AGO=1` により前日分のみを取得
- `last_processed_dates.json` にサイトごとの最後に処理した対象日を保存し、同日の重複実行を回避
- その日のGemini呼び出し数を `gemini_usage.json` で管理
- `gemini_usage.json` は古い履歴を自動間引きし、容量増加を抑制
- `GEMINI_DAILY_REQUEST_LIMIT` 到達後はGeminiを呼ばず、要約スキップのメッセージをSlackに送信
- `MAX_ARTICLES_PER_RUN` により、短時間に大量投稿があっても一度に使うAPI回数を抑制
- `GEMINI_MIN_INTERVAL_SECONDS=5` により、RPM 15制限を下回るペースで呼び出し
- `BLOG_SOURCES=hinatazaka,nogizaka` で日向坂46と乃木坂46の両方を対象にする

## 推奨設定（無料枠最優先）

- 実行頻度: 1日1回（例: 毎日 00:10）
- `TARGET_DAYS_AGO=1`
- `GEMINI_MIN_INTERVAL_SECONDS=5`（約12 RPM）
- `GEMINI_DAILY_REQUEST_LIMIT=200`（無料枠 1,500 RPD に対して十分な安全余裕）
- `MAX_ARTICLES_PER_RUN=200`

前日分だけ要約したい運用では、1日1回実行にすることで同日重複を避けつつ、無料枠超過リスクを最小化できます。

## 依存パッケージ

```bash
pip install -r requirements.txt
```

## 実行

```bash
python scraper.py
```
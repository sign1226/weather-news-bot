# weather-news-bot

Discord 天気予報ボット。毎朝6:00 に天気予報を投稿し、タイムカード機能も搭載。

## 機能

- **天気予報**: 毎朝6:00 に松本市 or 安曇野市の天気を投稿
- **1週間予報**: セレクトメニューで切り替え可能
- **お出かけ先対応**: カスタム地点の天気も取得可能
- **タイムカード**: 出勤/退勤の打刻、月間一覧表示
- **地震監視**: 5分ごとに有感地震を監視
- **台風監視**: 10分ごとに台風情報を監視

## 環境変数

`.env` ファイルをプロジェクトルートに作成：

```env
BOT_TOKEN=your_discord_bot_token
CHANNEL_ID=your_channel_id
```

## デプロイ

```bash
docker compose up -d
```

## ログ確認

```bash
docker compose logs --tail=50
```

## コード修正後の反映

```bash
docker compose down && docker compose up --build -d
```

## 使用API

- [Open-Meteo](https://open-meteo.com/) — 天気予報（APIキー不要）
- [気象庁](https://www.jma.go.jp/) — 警報・注意報、地震、台風情報

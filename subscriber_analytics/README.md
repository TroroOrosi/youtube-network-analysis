# サイレント登録者分析

自分の YouTube チャンネルの登録者のうち「コメントしていない登録者（サイレント登録者）」を、
**「直近3ヶ月以内の登録者」のような期間条件付き**で抽出するツール。
既存のネットワーク分析（リポジトリルート）とは独立した自己完結ディレクトリで、
既存ファイルには一切手を加えない。

## 仕組み（3ステージ）

```
[1] 登録者収集 (OAuth必須)         [2] コメント収集 (APIキー)        [3] 抽出 (ローカルのみ・クォータ0)
subscriptions.list                 playlistItems → commentThreads     レジストリ − コメント投稿者
(mySubscribers=true)               (+ comments.list で返信も取得)      の差集合 + 期間フィルタ
   │                                  │                                  │
   ▼                                  ▼                                  ▼
data/snapshots/*.csv               data/comments/{video_id}.csv       output/silent_subscribers_*.csv
   └→ data/subscriber_registry.csv    (動画別キャッシュ・スキップ可)      + 4象限サマリー表示
      (first_seen_at/last_seen_at)
```

- **スナップショット＋レジストリの二層構造**：実行のたびに登録者一覧をスナップショットとして
  追記保存し、`first_seen_at`（初観測）/ `last_seen_at`（最終観測）を持つレジストリへ集約する。
  API の返却は約1,000件が上限だが、**定期実行（週1推奨）でレジストリが上限を超えて育ち**、
  `first_seen_at` が登録日の観測ベースの近似になる。`last_seen_at` が古い人は解約の可能性がある
  （抽出は既定でこうした人を除外する。下記「期間条件の例」参照）。
- **直近登録者を優先取得**：`mySubscribers=true` は返却順が保証されないため、新しい順で返る
  `myRecentSubscribers=true` を併用して両方の結果を統合する。返却上限を超える規模の
  チャンネルでも「直近3ヶ月の登録者」が取りこぼされない。
- **コメントは初回に全履歴を収集**し、期間フィルタは抽出時に適用する。「一度もコメントして
  いない」の判定には全履歴が必要で、期間を変えた再抽出はクォータ消費ゼロで何度でもできる。
- **返信も収集する**：トップレベルコメントだけを見ると「返信でだけ交流している人」を
  サイレント扱いしてしまうため、`comments.list(parentId=...)` で返信も必ず取得する。

## セットアップ

```bash
pip install -r subscriber_analytics/requirements.txt
```

1. **OAuth クライアント（登録者収集に必要）**
   [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成し、
   *YouTube Data API v3* を有効化 → OAuth 同意画面を設定（テストユーザーに自分を追加）→
   認証情報で「OAuth クライアント ID（デスクトップアプリ）」を作成し、ダウンロードした JSON を
   `subscriber_analytics/client_secret.json` に保存する。
2. **API キー（コメント収集に必要）**
   同コンソールで API キーを作成し、環境変数 `YOUTUBE_API_KEY` に設定する（`.env` でも可。
   ルートのネットワーク分析と同じ方式）。

### GitHub Codespaces で使う場合

Codespaces のユーザー secret（対象リポジトリを `youtube-network-analysis` に許可）として、
次を設定する。値はリポジトリの Actions secret ではなく、Codespaces secret に置く。

| Secret | 必須 | 内容 |
|---|---:|---|
| `YOUTUBE_CHANNEL_ID` | 必須 | 誤アカウント取得を防ぐ対象チャンネルID（`UC...`） |
| `YOUTUBE_OAUTH_TOKEN_JSON` | 必須 | ローカルで初回OAuth認可後に生成された `data/token.json` のJSON全体 |
| `YOUTUBE_OAUTH_CLIENT_SECRET_JSON` | 任意 | client secret JSON全体。通常はローカルのファイルで初回認可する |
| `YOUTUBE_API_KEY` | 任意 | APIキー方式で公開動画だけを収集するとき。OAuth利用時は不要 |

`YOUTUBE_API_KEY` や Google Cloud / GitHub の管理者権限だけでは、
`subscriptions.list(mySubscribers=true)` は実行できない。対象チャンネルの実オーナーとして発行した
OAuth トークンが必要で、YouTube Studio の「チャンネル権限」で招待された管理者・編集者は
YouTube API ではそのチャンネルを代理操作できない。

設定後、秘密値を表示しない事前診断を行う（`--online` は約2 quota units）：

```bash
python subscriber_analytics/preflight.py
python subscriber_analytics/preflight.py --online
```

初回はローカルに `client_secret.json` を置き、対象IDを `YOUTUBE_CHANNEL_ID` に設定してから
次を実行する。ブラウザ認可後、**登録者一覧はまだ保存せず**、OAuth先と権限だけを検証して
`data/token.json` を生成する。このJSON全体を Codespaces の
`YOUTUBE_OAUTH_TOKEN_JSON` secret に登録する。

```bash
python subscriber_analytics/preflight.py --online --authorize
```

OAuth 同意画面がテスト状態なら refresh token は7日で失効し得る。その場合もローカルで同じ
コマンドを再実行し、Codespaces secret の token JSON を更新する。Codespaces 内では
localhost を使う対話OAuthを開始しない。

## 使い方

CLI とノートブックのどちらでも実行できる（同じ関数を呼ぶため結果は同一で、データも共有される）。

### ノートブックで実行する

[`subscriber_analytics.ipynb`](subscriber_analytics.ipynb) を開き、セルを上から順に実行する
（リポジトリルートの `youtube_network_analysis.ipynb` と同じ形式。前提条件は上記セットアップと同じ）。
Stage 2 の `MAX_VIDEOS` / `FORCE` や Stage 3 の期間フィルタ（`SUBSCRIBED_WITHIN` など）は
各セル冒頭の変数で指定する。

### CLI で実行する

```bash
# 0. OAuth先・対象チャンネル・mySubscribers権限を確認（取得データは保存しない）
python subscriber_analytics/preflight.py --online

# 1. 登録者スナップショット収集（事前診断合格後。週1などで定期実行を推奨）
python subscriber_analytics/collect_subscribers.py

# 2. 全動画のコメント収集（チャンネルIDは手順1が保存した値を自動利用。
#    手順1の OAuth トークンがあれば自動でオーナー権限収集となり、
#    非公開・限定公開の動画のコメントも対象になる）
python subscriber_analytics/collect_comments.py

# 3. 抽出：直近3ヶ月以内に登録したが一度もコメントしていない人
python subscriber_analytics/extract_silent.py --subscribed-within 90d --never-commented
```

`extract_silent.py` は誤判定防止のため、全動画のコメントキャッシュが揃うまで停止する。
`--max-videos` で分割しても、同じ値で再実行して全動画が揃えば抽出できる。既存キャッシュを
最新時点へ揃えたい場合は `collect_comments.py --force` で全動画を更新する。
APIキーによる公開動画だけの収集も既定では停止対象になる。サイレント判定には
`collect_comments.py --use-oauth --force` でオーナー範囲を収集する。
未完了データを承知で使う場合だけ、抽出に `--allow-incomplete-comments` を付ける
（偽陽性が生じ得る）。

### 期間条件の例（組み合わせは AND）

| やりたいこと | コマンド例 |
|---|---|
| 直近3ヶ月以内の登録者のみ | `--subscribed-within 90d`（`12w` / `3m` も可） |
| 特定期間に登録した人 | `--subscribed-since 2026-04-01 --subscribed-until 2026-06-30` |
| 一度もコメントしていない人 | `--never-commented` |
| 直近90日コメントしていない人（過去はあってもよい） | `--no-comment-within 90d` |
| 解約した可能性のある人も含める | `--include-unsubscribed`（既定は最新スナップショットに出現した現役登録者のみ） |
| フィルタなし | 全登録者を4象限セグメント付きで出力 |

出力 CSV の列: `channel_id, title, subscribed_at, subscribed_at_source, first_seen_at,
last_seen_at, comment_count, last_comment_at, segment`

- `subscribed_at_source` … 登録日として API の `publishedAt` を使ったか（`api_published_at`）、
  レジストリの初観測日時で代用したか（`first_seen_at`）を示す。`publishedAt` が実際の登録日時を
  指すかは API ドキュメント上保証がないため、判定根拠を常に明示する。
- `segment` … 4象限: **新規サイレント**（期間内登録・コメント0）/ **古参サイレント**
  （それ以前の登録・コメント0）/ **休眠**（過去コメントあり・期間内なし）/ **アクティブ**。

## 制約（結果の読み方）

- 取得できるのは**登録を公開しているユーザーのみ**。非公開登録者はどの API でも見えないため、
  抽出結果は常に**実際のサイレント登録者数の下限値**である。
- `subscriptions.list(mySubscribers=true)` の返却は**約1,000件が上限**。定期実行による
  レジストリ蓄積で緩和する（上限超のチャンネルでも観測集合が育つ）。
- OAuth 同意画面が**テストステータス**の場合、リフレッシュトークンは**7日で失効**する
  （失効時は自動でブラウザ再認証にフォールバック）。
- クォータ: `subscriptions` / `playlistItems` / `commentThreads` / `comments` いずれも
  1 unit/ページ（既定の日次上限 10,000 unit）。コメント収集は動画単位キャッシュで
  中断・再開できるため、大規模チャンネルでも複数日に分割して収集できる
  （`--max-videos N` は「その回に**新規収集**する動画数」の上限。キャッシュ済みは
  数えないため、同じ値のまま再実行すれば続きから N 本ずつ進む）。
- コメント収集を API キーのみで行うと**公開動画のみ**が対象になる（非公開・限定公開の
  動画にしかコメントしていない人をサイレント誤判定し得る）。OAuth トークンがあれば
  自動でオーナー権限収集に切り替わる（`--use-oauth` で強制も可）。
- ルートの `edges_anon.csv` はコメント投稿者IDを `u0, u1, ...` に不可逆に置換しており、
  元の投稿者ID対応表も保存していない。そのため、既存ネットワーク分析のコメント投稿者を
  登録者IDと突合してサイレント判定へ再利用することはできず、本ディレクトリで投稿者IDを
  コメント本文なしで再収集する必要がある。

## プライバシー

`data/` と `output/` には**実チャンネルID**が含まれるため `.gitignore` でコミット対象外に
している（コメント本文は保存しない）。第三者と共有する分析を行う場合は、ルートの
ネットワーク分析と同様に `u0, u1, …` 形式へ匿名化してから使うこと（リポジトリルートの
README「プライバシー・倫理」参照）。

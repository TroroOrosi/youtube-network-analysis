# 本番更新手順（2026-09-18）

## 完了の判定

GitHub のテスト合格、Docker の起動、本番への反映、所有者のチャンネル収集は別の確認です。
この手順の `SOURCE_AND_PUBLIC_SMOKE_VERIFIED` は、指定ソースの起動・ログイン画面・未認証アクセスの拒否までです。
Google ログイン、所有者の OAuth、実際の YouTube 収集、日跨ぎ再開は最後の受入確認で記録してください。

2026-09-18 14:02 UTC の読み取り専用確認では、既存サービスの `/login` は 200、`/` は `/login` へ 303。
稼働ソースの識別ヘッダーはなく、これだけでは PR #9/#10 の本番反映や収集復旧は確認できませんでした。
本変更から `/health/live`、`/health/ready`、`X-YNA-Revision`、`X-YNA-Source` を提供します。
これらのプローブは初期化済みの確認であり、現在の YouTube・Firestore・OAuth の正常性を保証しません。

## 修正範囲と保持するもの

収集中の再クリックは新しい収集を増やさず、既存ジョブの進捗へ戻します。
日次枠待機中の run ID、再試行日時、チェックポイントを捨てません。
進捗判定は履歴の表示上限と分離しました。2,100 件の完了履歴の後ろに未完了ジョブがあっても、完了扱いにしません。
CSRF、ワークスペースの権限分離、収集開始のレート制限、YouTube のクォータ制限は維持します。
既存の PR #9/#10 の重複コメント集約・大容量保存・日跨ぎ再開も引き続き使用します。

## 対応する運用形態

既存サービスの停止を伴う更新です。無停止のカナリア更新には対応しません。
現在の保存方式はプロセスごとの状態を丸ごと保存する単一 writer 構成です。
旧・新リビジョン、別サービス、ローカル復旧ツールを同じ本番データへ同時接続しないでください。
`max-instances=1` だけを排他制御とみなさないでください。今回、分散ロックや複数 writer 対応は追加していません。
Docker は明示的に Uvicorn **1 worker** で起動します。
更新ツールは最後にサービスを **manual scaling=1** にします。常時 1 インスタンスとなり、ゼロ円運用を保証しません。

前提：認証済み Google Cloud Shell（Linux、Python 3.12 以上、Git、現行 gcloud）。
Cloud Run 更新、サービスアカウント利用、ソースビルド、Firestore 読み取り、Scheduler 操作に必要な権限が必要です。
このツールは権限を自動追加せず、IAM、公開範囲、OAuth の秘密値を書き換えません。
Google Cloud の認証情報をチャットや GitHub に貼る必要はありません。

## 1. 停止前の検査（読み取りのみ）

この PR を含み、CI の全ジョブが通ったコミットを checkout してください。未コミット変更や未追跡ファイルは更新時に拒否します。
既存サービスのプロジェクト・リージョンを確認してから実行します。

```bash
PROJECT='snappy-byway-498603-v9'
REGION='asia-northeast1'
SERVICE='yna-web'
python3 -m scripts.release_production --project "$PROJECT" --region "$REGION" --service "$SERVICE" --check
```

省略時も `--check` 相当です。Cloud Run と Scheduler の設定を読み、秘密のペイロードは取得しません。
Scheduler が別リージョンなら `--scheduler-location` を明示してください。
出力の `revision`、`scheduler_name`、`base_url` を確認します。
このコマンドが合格しても、秘密の中身やサービスアカウントの実効権限までは確認済みになりません。

必須の起動設定：`YNA_BASE_URL`（HTTPS の origin）、`YNA_REQUIRE_GOOGLE=1`、
`YNA_GOOGLE_CLIENT_ID`、`YNA_GOOGLE_CLIENT_SECRET`、`YNA_FIRESTORE_DATABASE`、
`YNA_CREDENTIAL_SECRET`、`YNA_YOUTUBE_API_KEY`、`YNA_DRAIN_SERVICE_ACCOUNT`。
OAuth client secret と API key は既存の Secret Manager 参照を使います。
`YNA_STATE_DIR` の本番利用、複数 worker、独自コンテナ起動コマンドは拒否します。
本番 Cloud Run で設定不足のままデモへフォールバックすることはできません。

不足する設定がある場合、次のメンテナンス停止を先に行い、停止した状態で設定を補ってください。
以前の README の `--set-env-vars` / `--set-secrets` を更新に流用しないでください。既存設定を消します。
設定を更新した場合は新しいリビジョンになることがあるため、その後 `--check` をやり直します。

## 2. すべての writer を停止

利用者にメンテナンスを知らせ、ブラウザーの収集操作と別のデプロイを止めます。
出力された Scheduler の実際の job 名・location を使用します。下の `実際のジョブ名` は置き換えが必要です。

```bash
SCHEDULER_JOB='実際のジョブ名'
SCHEDULER_LOCATION="$REGION"
gcloud scheduler jobs pause "$SCHEDULER_JOB" --project "$PROJECT" --location "$SCHEDULER_LOCATION"
gcloud run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" --clear-tags
gcloud run services update "$SERVICE" --project "$PROJECT" --region "$REGION" --scaling=0
```

Scheduler が未作成なら pause の代わりに、不足設定を停止中に整えてから作成・pause します。
停止前に受け付けたリクエストは完了まで動くことがあります。
**Cloud Run の実行中リクエスト・ログを確認し、旧リクエスト、別サービス、ローカル復旧処理がすべて終了してから進めてください。**
サービス設定が 0 であることだけでは、書き込み終了の証明にはなりません。
タグ付き URL はサービスの scaling=0 だけでは停止しないため、タグ除去を省略しないでください。
外部 writer の不在はツールで完全には検出できません。`--writers-stopped` は確認済みの運用者による申告です。

不足していた Scheduler を作る場合は、同じ `/internal/drain` URI、`POST`、アプリ設定と同じ OIDC service account / audience を使います。
`YNA_DRAIN_AUDIENCE` の未指定時は **ベース URL + `/internal/drain`** が audience です。
Google Cloud 側のデフォルトと一致するとは限らないため、明示して合わせます。
HTTP の attempt deadline は 180 秒以上、呼び出し間隔は 5 分以上を目安に、まず旧ジョブを重複作成しないことを確認してください。
ログイン用 OAuth の callback は `/login/callback`、チャンネル認可用は `/oauth/callback` です。
Google アカウントのログイン成功と YouTube チャンネルの再認可は別です。

## 3. バックアップして指定ソースを更新

全 writer 停止後にもう一度 `--check` が合格することを確認します。
`REVISION` はその時点の出力値に固定します。更新ツールは相違があれば中断します。

```bash
REVISION=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.latestReadyRevisionName)')
BACKUP_DIR="$HOME/yna-backup-$(date -u +%Y%m%dT%H%M%SZ)"
python3 -m scripts.release_production \
  --project "$PROJECT" --region "$REGION" --service "$SERVICE" \
  --expected-revision "$REVISION" --writers-stopped \
  --backup-directory "$BACKUP_DIR" --apply
```

このコマンドは以下を順に行います。

1. 単一リビジョン、manual scaling=0、タグなし、Scheduler 停止、必須設定を検査。
2. `workspace_access` / `channel_connections` / `channel_data` / `collection_jobs` / `analysis_api` の論理スナップショットを保存。再読み込みして SHA-256 が変わっていないことを検査。
3. 既存データが欠けた別データベースへの誤接続を拒否。サービス設定の変化も再検査。
4. Git の clean archive だけをビルドに送り、コミット SHA をイメージ内へ固定。バックアップや作業中のファイルは送らない。
5. 既存の環境変数・Secret Manager 参照を維持して、停止状態のまま新リビジョンを作成。
6. 指定した新リビジョンのみにトラフィックを固定し、1 インスタンスで有効化。
7. 実際の source SHA / revision / Google モード、ログイン画面、未認証ホーム・drain の拒否を確認してから Scheduler を再開。

バックアップディレクトリは 0700、ファイルは 0600 で作成します。
**トークン本体を含まなくてもユーザー・セッション情報を含みます。チャット、PR、公開ストレージへ添付しないでください。**
これは 5 モジュールの状態バックアップであり、Secret Manager の資格情報・IAM 設定を含む完全な災害復旧バックアップではありません。
既存の credential secret は変更せず保持します。復旧時には資格情報との対応も別途確認してください。

途中で失敗した場合は Scheduler を再開せず停止状態を保って調べます。
有効化後の検査失敗ではツールが停止を試みますが、ネットワーク障害等で停止確認が失敗した場合は Cloud console で直ちに状態を確認してください。
古いコードを新しいスナップショットに接続する自動ロールバックは行いません。
バックアップからの復元も、全 writer 停止とスキーマ・資格情報の整合確認なしには実行しないでください。

## 4. 所有者による受入確認（省略不可）

本番 URL で Google ログインし、以前のワークスペース・チャンネル・保存済みデータが見えることを確認します。
期限切れ・権限不足の場合はチャンネルの再認可を使用します。データを消すための切断・ワークスペース削除は不要です。
既存の収集中なら進捗を再開し、旧版で FAILED になった場合は対象チャンネルの新規収集を一度開始します。
旧 RUNNING の厳密な対象限定復旧が必要なら [owner-content-recovery.md](owner-content-recovery.md) の plan を先に確認してください。
旧 RUNNING を条件なしに QUEUED に書き戻してはいけません。

確認する項目は、開始の再クリックで 409 にならないこと、進捗が増えること、実際の収集結果・分析・CSV が開けること、
別ユーザーのワークスペースが見えないこと、ブラウザーを閉じても Scheduler の認証付き呼び出しが成功して処理が進むことです。
日次枠到達は成功と表示せず、次回日時を保持した待機となることを確認します。
クォータを使い切るためだけの本番テストは行わず、境界は CI で検証し、実際の発生時に再開を確認してください。
アプリの独自 1,000 件制限と、YouTube から公開・取得可能な登録者数は別です。登録者全員の取得を受入条件にしないでください。

再発調査にはレスポンスの `X-YNA-Request-ID` とアプリログの `request_rejected` を対応させます。
新しい診断ログには HTTP status、内部 error code、ルートのテンプレート、revision/source を記録し、
OAuth code、Cookie、認証ヘッダー、フォーム内容、URL 内の個別識別子は記録しません。
ヘルスチェック成功だけで「収集復旧済み」と報告しないでください。

## 公式資料

- [Cloud Run manual scaling とサービス停止](https://docs.cloud.google.com/run/docs/configuring/services/manual-scaling)
- [gcloud run deploy：no-traffic / update-env-vars / set-env-vars](https://docs.cloud.google.com/sdk/gcloud/reference/run/deploy)
- [Cloud Run の revision traffic tag](https://docs.cloud.google.com/sdk/gcloud/reference/run/services/update-traffic)
- [Cloud Scheduler HTTP target の認証](https://docs.cloud.google.com/scheduler/docs/http-target-auth)

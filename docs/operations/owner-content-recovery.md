# 停止した動画・コメント収集の限定復旧

## 目的と変更範囲

`web_ui/recover_owner_content.py` は Cloud Shell / Python 3.11 以降で使う、明示的な管理作業用の単独スクリプトです。通常のWebアプリからは呼びません。

デフォルトは読み取り専用 PLAN。`--apply` は以前に認可された一つの `OWNER_CONTENT` ジョブを失敗履歴として残し、別IDの `QUEUED` を一件作成します。作成したジョブはサービスと既存のSchedulerを再開すると実行され得ます。スクリプト自身はYouTube APIを呼びません。

- 書き込み先は `state/collection_jobs` の `document` フィールドだけです。
- 既存の登録者ジョブ、日次利用量、他チャンネルのジョブは変更しません。
- `channel_data` の登録者・採用済みデータを読み書きしません。
- `workspace_access` は所有者照合に読むだけです。メンバー追加、権限昇格、セッション期限の延長、ログインの代行は行いません。
- `channel_connections` は既存接続の照合だけに使います。Secret Manager、Google更新トークン、APIキーを読み出しません。
- 旧動画収集の `channel_data.collections` に残る `IN_PROGRESS` は変更しません。新規ジョブは別の収集IDを使うため衝突せず、旧行は未完了の履歴として残ります。それを `COMPLETE` に書き換えてはいけません。

Cloud IAMで認可された運用者による保守であり、運用者にアプリ内の他者ワークスペースの閲覧権限を恒久付与する機能ではありません。サービス再開後の実行には既存のワークスペース限定ジョブ権限とGoogle認可が使われます。

## 準備

対象の識別子だけを `target.json` に保存します。実ユーザーの設定ファイルはリポジトリ外で管理し、コミットしないでください。メールやセッション値は不要です。

```json
{
  "project": "YOUR_PROJECT",
  "region": "asia-northeast1",
  "service": "YOUR_SERVICE",
  "workspace_id": "workspace_TARGET",
  "channel_id": "UC_TARGET",
  "connection_id": "connection_TARGET",
  "source_run_id": "run_STALLED",
  "owner_user_id": "user_EXISTING_OWNER"
}
```

Cloud Shellへスクリプトと設定ファイルをアップロードします。Cloud Runのサービス・リビジョン参照、Firestore文書参照・更新・トランザクションに必要な既存IAM権限を持つ管理者として実行してください。403で拒否されたときは権限を確認し、サイトの認証を回避するための資格情報を作らないでください。

```bash
python3 recover_owner_content.py --target target.json
```

リポジトリのルートから実行する場合はスクリプト名を `web_ui/recover_owner_content.py` に置き換えます。追加pip依存関係は不要です。`gcloud auth print-access-token` の出力は内部で取得し、画面やバックアップへ出しません。

`READY_TO_APPLY` は本番変更前の計画であり、再収集開始・完了の証明ではありません。`serving_revision` は実際に100%のトラフィックを受けるリビジョン、`database` はそのリビジョンの `YNA_FIRESTORE_DATABASE` です。最新作成リビジョンで代用しません。

## 必須の停止・デプロイ確認

このアプリは全体状態をメモリーに読み込む単一ライター方式です。Firestoreの競合検知だけでは、修復後に古いプロセスが状態を上書きするのを防げません。**稼働中に実行しないでください。**

1. 対象を再確認し、関係する自動実行・デプロイを停止します。現状のスケーリング設定、トラフィック、Schedulerの状態を控えます。
2. 対象サービスへトラフィックタグが付いている場合は解除し、そのリビジョンも停止します。手動0台はタグだけで生きているリビジョンを停止しないため、ツールはタグがあれば拒否します。
3. サービスを手動0台にします。以下の `$PROJECT`、`$REGION`、`$SERVICE` は設定に合わせます。

   ```bash
   gcloud run services update "$SERVICE" --project="$PROJECT" --region="$REGION" --scaling=0
   ```

   これは最小インスタンスを0にする設定とは異なります。サイトは一時的に利用不可になります。既存リクエストは完了するまで動けるので、コンテナ数（active/idleとも）とログで旧インスタンス・処理が終了したことを確認します。Cloud Run Jobs、別サービス、ローカルプロセスなど同じDBへ書くものもすべて停止します。
4. **PR #9のページ再開・重複行対策・保存対策を含むコードを本番に反映済みであることを確認します。** ソースのマージだけでは不十分です。未反映なら、先に [保存形式移行の運用手順](2026-09-16-availability-large-channels.md) の整合バックアップ・旧新ライター切替手順でデプロイし、その後もう一度停止状態を確認します。旧版のまま再受付しません。新旧リビジョンが同じDBに書くカナリア展開は禁止です。このツールのjobs単体バックアップは、PR #9の保存形式移行用の全体バックアップの代用ではありません。
5. そのコードを確認したリビジョン名を `VERIFIED_REVISION` に設定します。ツールは一致を照合しますが、コンテナの中身や全インスタンスの停止を自動証明しません。`--writers-stopped` と `--patched-code-verified` は、それぞれ運用者による明示的な確認です。

既知の旧リビジョン `yna-web-00021-mil` に今回の収集修正が含まれるとみなしてはいけません。停止・再開の操作そのものは、このスクリプトが自動実行するものではありません。

## 適用

```bash
python3 recover_owner_content.py --target target.json \
  --apply --expected-revision "$VERIFIED_REVISION" \
  --writers-stopped --patched-code-verified
```

接続・所有者・対象ジョブをライブのFirestoreから再確認した後、jobsの原文を `~/.yna-recovery-backups/` に保存します。ディレクトリ0700・ファイル0600で、作成・書き込みに失敗すれば本番へのコミット前に止まります。アクセス用JSON、セッション、メール、Google資格情報はバックアップしません。

Firestoreのread-writeトランザクション内で三つの文書を読み、jobsの更新日時にも前提条件を付けて更新します。対象以外の更新を混ぜません。コミット後に読み直して一致したときだけ `outcome: QUEUED` / `verified: true` を表示します。この `verified` は受付データの保存確認であり、YouTube収集成功の確認ではありません。

**`COMMIT_OUTCOME_UNCONFIRMED` になったら、そのまま昔のデータを復元しないでください。** 応答消失で保存だけ成功した可能性があります。デフォルトPLANを実行し直して、同じ `replacement_run_id` があるか確認します。結果不明の書き込みは自動再試行しません。

置換IDは元ジョブと対象から固定的に生成します。同じ対象へ再実行しても新規ジョブを増やさず、`ALREADY_PREPARED` と置換ジョブの現在状態を返します。置換ジョブが後に失敗しても、このツールの繰り返しで自動的にさらに再収集を作ることはありません。

## サービスと処理の再開

準備が完了したら、控えたスケーリング方式に戻します。元が自動スケーリングなら：

```bash
gcloud run services update "$SERVICE" --project="$PROJECT" --region="$REGION" --scaling=auto
```

元が手動設定の場合は元の値に戻します。単一インスタンス・単一プロセス制約を維持し、障害回避のためにインスタンス数を増やさないでください。停止した既存Schedulerは、対象が同じサービスの `/internal/drain` であること、OIDC設定が維持されていることを確認してから再開します。リポジトリの過去運用記録での名称は `yna-drain` ですが、現在のジョブ名・対象・停止前の状態を優先し、停止前から無効なジョブを勝手に有効化しません。

```bash
gcloud scheduler jobs list --project="$PROJECT" --location="$REGION" \
  --format='table(name,state,httpTarget.uri)'
# 一時停止した該当ジョブだけをresume。ジョブがなければ新規作成はしない。
```

Schedulerがない場合は、ワークスペースの所有者が通常のログイン後に収集画面を開くか、許可済みの運用手順でドライバーを動かす必要があります。`QUEUED`を入れるだけではワーカーのないシステムで処理は進みません。

PLANを再実行し、置換ジョブの現在状態を確認できます。原則、収集ページ数の増加と最終 `SUCCEEDED`、`channel_data` の動画採用・コメントcoverage、分析表示まで確認します。途中のQUEUEDや、片方の収集完了だけで分析全体の成功としません。

Google側の更新用認可が失効していた場合は、接続・ジョブが`REAUTH_REQUIRED`になることがあります。本人の再認可が必要です。`workspace_access.sessions` の有効期限を延長してもGoogle認可は復活しません。既存の接続を解除するとデータ削除につながるので解除しません。

## 対応範囲と中止条件

- 対応する入力は access/connections v1、jobs v1/v2の単独文書です。`parts > 1` / `generation` のある文書や未知バージョンは変更せず拒否します。大容量 `channel_data` は触らないため、そちらが分割されていること自体は支障ありません。
- 1時間以内の実行、正常終了済み・取消済み、途中再開情報のあるジョブ、別のアクティブ／新しい動画・コメントジョブがある場合は拒否します。
- 古いジョブは失敗として保存し、利用量39と個別ジョブ合計との差など不確実な値を推測で補いません。
- サービスの再有効化・タグ追加などを別の管理者が同時に行わないよう調整が必要です。DBトランザクションとCloud Run設定変更は一つの原子的操作ではありません。
- 読み書き料金と再開後のYouTube API枠は既存の課金・制限の対象です。
- このツールのテストは合成データ／提供スナップショットのオフライン検証です。実GCPへの反映や実YouTube収集成功を検証済みとはしていません。

## テスト

```bash
python -m unittest discover -s web_ui/tests -p test_owner_content_recovery.py -v
```

副作用のない変換、別対象拒否、コード／停止確認拒否、所有者照合、二重受付防止、バックアップ失敗、トランザクションと更新前提条件、応答消失時の処理、既存ドメインcodecでの読込を検証します。通常CIの `web_ui/tests` に含まれます。

公式仕様（2026-09-17確認）：
- https://docs.cloud.google.com/run/docs/configuring/services/manual-scaling
- https://docs.cloud.google.com/firestore/docs/reference/rest/v1/projects.databases.documents/get
- https://docs.cloud.google.com/firestore/docs/reference/rest/v1/projects.databases.documents/commit
- https://docs.cloud.google.com/firestore/docs/reference/rest/v1/Write

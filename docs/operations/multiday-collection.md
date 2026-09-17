# 登録者・動画・コメントの日跨ぎ収集

## 今回変わるもの／変えられないもの

新規のWeb登録者収集は`myRecentSubscribers`から`mySubscribers`へ変更し、APIが返す
`nextPageToken`がなくなるまで取得します。コード調査ではもともと1,000行・20ページで
強制終了する総件数制限はありませんでした。APIの一回50件はページサイズであり、
総件数の上限ではありません。合成応答1,251件・26ページの取得で打ち切りなしを検証します。

YouTube公式は両方の登録者APIについて返却件数を制限する可能性を記載しています。
`mySubscribers`に変えるだけで必ず1,000件を超える、非公開登録者を含め全員を取れる、
有料Cloudアカウントなら制限を解除できる、とは言えません。APIが続きを返さなければ
そこで提供範囲の取得は終了します。`COMPLETE` / `SUCCEEDED`は全登録者の網羅保証ではありません。
分析画面にその制限を表示し、既存のPUBLIC_SUBSCRIPTIONS_ONLYとPROVIDER_RESULT_CAP_POSSIBLEを維持します。

## 日次上限と再開

| 上限 | 動作 | 再開可能時刻 |
|---|---|---|
| アプリのワークスペース別日次予算 | QUEUED、同一ジョブ・試行・ページ位置・取得データを保持 | 翌UTC日付の0時（日本9時） |
| YouTubeのquotaExceeded / dailyLimitExceeded | Google認可を失効扱いにせず同様に待機 | 次の米国太平洋時間0時（日本16時／17時、夏冬時間対応） |

登録者・動画一覧・各動画内のコメントページがすべて対象です。予算不足だけを理由に
3日で破棄しません。次回時刻より前のdrainでは再取得しません。旧日次台帳はUTCのまま
維持し、Googleのプロジェクト全体の残高と同一視しません。ほかのワークスペースや
クライアントの消費分をこの台帳だけから正確に把握するものではありません。

失敗したAPI要求も利用量に含めます。動画一覧でuploads情報を取得してからページ要求が
quota拒否された場合も、その両方を計上します。取得できなかったページはページ数に足さず、
回復後に同じページから再試行します。403一般・再認可・不正カーソルをquota待ちに偽装しません。

既存のScheduler / 認可済みドライバーが稼働していれば、次回時刻以降の呼び出しで自動継続します。
Scheduler自体を新規作成・有効化する変更は行いません。未設定なら所有者の進捗ページから
継続が必要です。ホームにはページ数・次回実行可能時刻（日本時間）と継続リンクがあります。

## 保存データの扱い

途中の登録者はjobsのPageCheckpointへ保存し、最終ページまで完了するまでは
既存の採用済み1,000件を置き換えません。完了後に登録者スナップショットを採用し、
既存の累積登録者台帳へ重複排除して反映します。これは複数日にまたがる観測であり、
ある一瞬の全登録者一覧を原子的に取得する保証ではありません。
動画一覧が揃ってからコメント段階へ進み、コメントは同じ動画一覧を全てカバーするまで未準備です。
取消ではチェックポイントを削除します。認可失効時も既に記録したページ数・利用量は失いません。

新しい登録者カーソルには `yna:subscribers:v1:` を付けます。APIへ送る前に除去し、
`mySubscribers`にだけ使用します。包絡で既存の256文字カーソルが拒否されないよう、
providerページトークンだけ別の512文字上限に分離し、リソースIDの256文字制限は維持します。
旧版が保存したプレーンなカーソルは従来の
`myRecentSubscribers`で継続し、異なるクエリへ流用しません。API側のカーソル失効や
結果の変動を完全に防ぐことはできません。旧版へ戻して新カーソルを送信しないでください。

jobsスナップショットはv2を維持し、旧v1も読めます。全状態のメモリー保持・JSON全体再保存・
単一ライターという構成は変わりません。大規模化に伴うメモリー／Firestore料金・I/O制約は残ります。

## 本番反映と内田氏チャンネルの再受付

1. [前回の移行手順](2026-09-16-availability-large-channels.md)に従い、既存データの
   整合バックアップと全ライター停止を確認します。新旧リビジョンの同時書き込みを避けます。
2. この更新を含むコードと`web_ui/requirements.txt`（tzdataを含む）をCloud Runへ反映します。
   PRのマージや単独の復旧スクリプト実行だけでは、実行中のランタイムは更新されません。
3. [限定復旧手順](owner-content-recovery.md)のPLANでライブの対象・ジョブを照合します。
   登録者も新規収集する今回の操作では`--include-subscribers`をPLAN/APPLY両方に付けます。
4. サービス停止確認後のAPPLYで、動画・コメントと登録者の待機ジョブを重複なく受け付けます。
   保存済みデータ・所有者権限・認可情報は変更しません。旧OWNER_CONTENTの置換がある場合は
   登録者ジョブだけを追加します。別の新しい収集があれば拒否します。
5. 元のサービス・既存Schedulerを再開し、新しい二つのジョブの状態、取得ページ数、
   次回時刻、最終SUCCEEDED、動画／コメントcoverageと分析結果を確認します。
   Google側の更新認可が失効していれば本人の再認可が必要です。

本作業から本番Firestore・Cloud Run・課金・YouTube APIへ変更／収集は実行していません。
配布ZIPはCloud Shell用復旧ツールで、アプリのデプロイを代行するものではありません。

## 回帰検証

```bash
python -m pip install -r web_ui/requirements-dev.txt -r subscriber_analytics/requirements.txt pytest openpyxl ruff
python -m pytest -q --import-mode=importlib analysis_api/tests channel_connections/tests channel_data/tests collection_jobs/tests subscriber_analytics/tests web_ui/tests workspace_access/tests
node --test web_ui/tests/collecting.test.cjs
```

新規テストは1,251行の実アダプター経路（合成HTTP）、登録者2,051件を5日間に分けた保存と
再起動、動画151本の一覧分割とコメント段階、各段階のprovider日次拒否、取消、3日超の
予算不足、太平洋時間の夏冬時間切替、再認可後の履歴保護、UI、追加登録者再受付を対象とします。
実際のYouTubeチャンネルで1,000件超の取得成功を確認したテストではありません。

## 一次資料（2026-09-17確認）

- 登録者feed・返却上限の注意・ページング：https://developers.google.com/youtube/v3/docs/subscriptions/list
- 日次quotaの太平洋時間リセット・失敗要求の利用量：https://developers.google.com/youtube/v3/determine_quota_cost
- 標準ライブラリのzoneinfoとOS／tzdata：https://docs.python.org/3/library/zoneinfo.html

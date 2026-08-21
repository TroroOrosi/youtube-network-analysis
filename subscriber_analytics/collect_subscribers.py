"""登録者スナップショット収集 CLI（OAuth 必須）。

subscriptions.list(mySubscribers=true) で「自分のチャンネルの公開登録者」を全ページ
取得し、次の2層に保存する。

  1. data/snapshots/subscribers_<UTC時刻>.csv … 実行ごとの生スナップショット（追記のみ）
  2. data/subscriber_registry.csv … first_seen_at / last_seen_at を持つ累積レジストリ

API の返却は約1,000件が上限だが、定期実行（週1など）でスナップショットを積むほど
観測集合が上限を超えて育ち、first_seen_at が「登録日」の観測ベースの近似になる。
API の snippet.publishedAt は api_published_at 列にそのまま記録する（登録日時を指す
かの信頼性が不明なため、抽出時にどちらを使ったかを明示する設計）。

使い方:
    python subscriber_analytics/collect_subscribers.py
    python subscriber_analytics/collect_subscribers.py --rebuild   # スナップショットから再集約
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SNAPSHOT_COLUMNS = ["channel_id", "title", "api_published_at", "snapshot_at"]
REGISTRY_COLUMNS = [
    "channel_id",
    "title",
    "api_published_at",
    "first_seen_at",
    "last_seen_at",
    "snapshot_count",
]


def fetch_subscriber_rows(youtube, snapshot_at: str):
    """公開登録者を全ページ取得してスナップショット行にする。戻り値は (rows, APIページ数)。

    mySubscribers=true は返却順が保証されず、返却上限を超えるチャンネルでは
    直近の登録者を取りこぼし得る。そのため新しい順で返る myRecentSubscribers=true
    を先に取得し、両方の結果を channel_id で重複排除して合算する
    （--subscribed-within での「直近登録者」分析の取りこぼしを防ぐ）。
    """
    resource = youtube.subscriptions()
    rows, pages, seen = [], 0, set()
    for selector in ({"myRecentSubscribers": True}, {"mySubscribers": True}):
        request = resource.list(
            part="subscriberSnippet,snippet",
            maxResults=50,
            **selector,
        )
        for response in common.iter_pages(resource, request):
            pages += 1
            for item in response.get("items", []):
                sub = item.get("subscriberSnippet", {})
                channel_id = sub.get("channelId", "")
                if not channel_id or channel_id in seen:
                    continue
                seen.add(channel_id)
                rows.append(
                    {
                        "channel_id": channel_id,
                        "title": sub.get("title", ""),
                        "api_published_at": item.get("snippet", {}).get("publishedAt", ""),
                        "snapshot_at": snapshot_at,
                    }
                )
    return rows, pages


def load_registry(path: Path) -> pd.DataFrame:
    if not Path(path).exists():
        return pd.DataFrame(columns=REGISTRY_COLUMNS)
    return pd.read_csv(path, dtype=str).fillna("")


def update_registry(registry: pd.DataFrame, snapshot: pd.DataFrame) -> pd.DataFrame:
    """スナップショット1回分をレジストリへ畳み込む（純粋関数）。

    既知の登録者は last_seen_at / snapshot_count を更新し、first_seen_at は保持する。
    api_published_at は初observed値を保持し、空だった場合のみ埋める。
    """
    entries = {row["channel_id"]: dict(row) for _, row in registry.iterrows()}
    for _, row in snapshot.drop_duplicates("channel_id").iterrows():
        channel_id = row["channel_id"]
        if channel_id in entries:
            entry = entries[channel_id]
            entry["title"] = row["title"] or entry["title"]
            if not entry.get("api_published_at"):
                entry["api_published_at"] = row["api_published_at"]
            entry["last_seen_at"] = row["snapshot_at"]
            entry["snapshot_count"] = int(entry.get("snapshot_count") or 0) + 1
        else:
            entries[channel_id] = {
                "channel_id": channel_id,
                "title": row["title"],
                "api_published_at": row["api_published_at"],
                "first_seen_at": row["snapshot_at"],
                "last_seen_at": row["snapshot_at"],
                "snapshot_count": 1,
            }
    result = pd.DataFrame(list(entries.values()), columns=REGISTRY_COLUMNS)
    return result.sort_values(
        ["first_seen_at", "channel_id"], ascending=[False, True]
    ).reset_index(drop=True)


def rebuild_registry(data_dir: Path) -> pd.DataFrame:
    """全スナップショットを時系列順に畳み込んでレジストリを作り直す（復旧用）。"""
    registry = pd.DataFrame(columns=REGISTRY_COLUMNS)
    files = sorted(common.snapshot_dir(data_dir).glob("subscribers_*.csv"))
    if not files:
        raise SystemExit(
            "スナップショットがありません。先に collect_subscribers.py を（--rebuild なしで）実行してください。"
        )
    for path in files:
        snapshot = pd.read_csv(path, dtype=str).fillna("")
        registry = update_registry(registry, snapshot)
    return registry


def run(youtube, data_dir: Path, now=None) -> dict:
    """収集1回分を実行し、スナップショット保存とレジストリ更新を行う。"""
    now = now or common.utcnow()
    snapshot_at = common.format_ts(now)
    rows, pages = fetch_subscriber_rows(youtube, snapshot_at)
    snapshot = pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS)

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    snapshot_file = common.snapshot_dir(data_dir) / f"subscribers_{stamp}.csv"
    common.atomic_write_csv(snapshot, snapshot_file)

    reg_path = common.registry_path(data_dir)
    registry = load_registry(reg_path)
    known = set(registry["channel_id"])
    new_count = sum(1 for r in rows if r["channel_id"] not in known)
    registry = update_registry(registry, snapshot)
    common.atomic_write_csv(registry, reg_path)

    return {
        "fetched": len(snapshot),
        "new": new_count,
        "registry_total": len(registry),
        "pages": pages,
        "snapshot_file": snapshot_file,
    }


def resolve_my_channel_id(youtube) -> str:
    response = youtube.channels().list(part="id", mine=True).execute()
    items = response.get("items", [])
    if not items:
        raise SystemExit(
            "OAuth ユーザーに API から操作可能な YouTube チャンネルがありません。"
            "YouTube Studio の委任権限は YouTube API では利用できません。"
        )
    return items[0]["id"]


def main() -> None:
    parser = argparse.ArgumentParser(description="公開登録者のスナップショット収集（OAuth）")
    parser.add_argument(
        "--client-secret",
        type=Path,
        default=common.DEFAULT_CLIENT_SECRET,
        help=f"OAuth クライアントシークレット JSON（既定: {common.DEFAULT_CLIENT_SECRET}）",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=common.DATA_DIR,
        help=f"データ保存先（既定: {common.DATA_DIR}）",
    )
    parser.add_argument(
        "--expected-channel-id",
        help="OAuth先と一致すべきチャンネルID（既定: YOUTUBE_CHANNEL_ID / 保存済みchannel_id.txt）",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="APIを呼ばず、既存スナップショット全件からレジストリを再構築する",
    )
    args = parser.parse_args()

    if args.rebuild:
        registry = rebuild_registry(args.data_dir)
        common.atomic_write_csv(registry, common.registry_path(args.data_dir))
        print(f"レジストリを再構築しました: {len(registry)} 人")
        return

    youtube = common.build_oauth_client(
        client_secret=args.client_secret,
        token_file=common.token_path(args.data_dir),
    )

    channel_id = resolve_my_channel_id(youtube)
    common.load_environment()
    saved_channel = common.channel_id_path(args.data_dir)
    saved_expected = (
        saved_channel.read_text(encoding="utf-8").strip() if saved_channel.exists() else ""
    )
    expected = (
        args.expected_channel_id
        or os.environ.get("YOUTUBE_CHANNEL_ID", "")
        or saved_expected
    )
    if not expected:
        raise SystemExit(
            f"OAuth先は {channel_id} です。誤取得防止のため、YOUTUBE_CHANNEL_ID または "
            "--expected-channel-id にこのチャンネルIDを設定してから再実行してください。"
        )
    if expected and channel_id != expected:
        raise SystemExit(
            f"対象チャンネル不一致: expected={expected}, OAuth={channel_id}。"
            "登録者データは保存していません。OAuthアカウントを確認してください。"
        )
    common.atomic_write_text(channel_id + "\n", saved_channel)
    print(f"自チャンネルID: {channel_id}（collect_comments.py が既定値として利用します）")

    stats = run(youtube, args.data_dir)
    print(f"スナップショット保存: {stats['snapshot_file']}")
    print(
        f"取得 {stats['fetched']} 人（新規 {stats['new']} 人）/ "
        f"レジストリ累計 {stats['registry_total']} 人 / "
        f"クォータ消費 約{stats['pages'] + 1} unit"
    )
    print(
        "注意: 取得できるのは登録を公開しているユーザーのみです（結果は常に下限値）。"
        "API の返却は約1,000件が上限のため、定期実行してレジストリを育ててください。"
    )


if __name__ == "__main__":
    main()

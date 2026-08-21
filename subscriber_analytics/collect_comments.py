"""自チャンネル全動画のコメント収集 CLI。

チャンネルの uploads プレイリストから全動画を列挙し、動画ごとにコメントを
data/comments/<video_id>.csv へ保存する。

認証は次の順で決まる:
  1. --use-oauth 指定時は常に OAuth（オーナー権限。非公開・限定公開の動画も対象）
  2. --api-key 未指定で OAuth トークン（collect_subscribers.py が保存）が
     あれば自動で OAuth を使う。APIキーのみだと公開動画しか列挙されず、
     非公開動画にしかコメントしていない人をサイレント誤判定し得るため
  3. それ以外は APIキー（環境変数 YOUTUBE_API_KEY / --api-key）

設計上のポイント:
  - トップレベルコメントに加えて**返信も必ず取得**する。commentThreads が同梱する
    返信は最大5件のため、totalReplyCount がそれを超えるスレッドは comments.list
    (parentId=...) で残りを取得する（返信のみのユーザーをサイレント誤判定しないため）。
  - 初回は全履歴を収集する。「一度もコメントしていない」判定には全履歴が必要で、
    期間フィルタは抽出時（extract_silent.py）に適用するため、期間を変えた再抽出は
    クォータ消費ゼロで行える。
  - 動画単位のキャッシュ: 保存済み動画はスキップする（--force で再取得）。クォータ
    上限（既定 10,000 unit/日）に達しても、翌日そのまま再実行すれば続きから収集できる。
  - コメント無効の動画は空の CSV をマーカーとして残し、以後スキップする。
  - プライバシー配慮のためコメント本文は保存しない（投稿者IDと日時のみ）。

使い方:
    python subscriber_analytics/collect_comments.py                # channel_id.txt / 環境変数から解決
    python subscriber_analytics/collect_comments.py --channel-id UCxxxxxxxx
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
from googleapiclient.errors import HttpError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

COMMENT_COLUMNS = [
    "video_id",
    "comment_id",
    "parent_id",
    "author_channel_id",
    "author_name",
    "published_at",
    "is_reply",
]


def _comment_row(video_id: str, comment: dict, is_reply: bool) -> dict:
    snippet = comment.get("snippet", {})
    return {
        "video_id": video_id,
        "comment_id": comment.get("id", ""),
        "parent_id": snippet.get("parentId", "") if is_reply else "",
        "author_channel_id": (snippet.get("authorChannelId") or {}).get("value", ""),
        "author_name": snippet.get("authorDisplayName", ""),
        "published_at": snippet.get("publishedAt", ""),
        "is_reply": 1 if is_reply else 0,
    }


def _is_comments_disabled(error: HttpError) -> bool:
    """コメント無効(403 commentsDisabled)か判定する。

    エラー理由は error_details の reason フィールドに入る（message 文字列には
    "commentsDisabled" が含まれない）ため、reason を直接確認する。
    """
    if getattr(error.resp, "status", None) != 403:
        return False
    details = getattr(error, "error_details", None) or []
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and detail.get("reason") == "commentsDisabled":
                return True
    content = getattr(error, "content", b"") or b""
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    return b"commentsDisabled" in content


def get_uploads_playlist(youtube, channel_id: str) -> str:
    response = youtube.channels().list(part="contentDetails", id=channel_id).execute()
    items = response.get("items", [])
    if not items:
        raise SystemExit(f"チャンネルが見つかりません: {channel_id}")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def list_uploaded_videos(youtube, playlist_id: str):
    """uploads プレイリストの全動画を (rows, APIページ数) で返す。"""
    resource = youtube.playlistItems()
    request = resource.list(
        part="snippet,contentDetails",
        playlistId=playlist_id,
        maxResults=50,
    )
    videos, pages = [], 0
    for response in common.iter_pages(resource, request):
        pages += 1
        for item in response.get("items", []):
            video_id = item.get("contentDetails", {}).get("videoId", "")
            if video_id:
                videos.append(
                    {
                        "video_id": video_id,
                        "title": item.get("snippet", {}).get("title", ""),
                    }
                )
    return videos, pages


def fetch_all_replies(youtube, parent_id: str):
    """スレッドの全返信を comments.list(parentId=...) で取得する。"""
    resource = youtube.comments()
    request = resource.list(
        part="snippet",
        parentId=parent_id,
        maxResults=100,
        textFormat="plainText",
    )
    replies, pages = [], 0
    for response in common.iter_pages(resource, request):
        pages += 1
        replies.extend(response.get("items", []))
    return replies, pages


def fetch_video_comments(youtube, video_id: str):
    """動画1本の全コメント（返信込み）を取得する。

    戻り値は (rows, APIページ数, コメント無効フラグ)。
    """
    resource = youtube.commentThreads()
    request = resource.list(
        part="snippet,replies",
        videoId=video_id,
        maxResults=100,
        textFormat="plainText",
    )
    rows, pages = [], 0
    try:
        for response in common.iter_pages(resource, request):
            pages += 1
            for item in response.get("items", []):
                thread_snippet = item.get("snippet", {})
                top = thread_snippet.get("topLevelComment", {})
                rows.append(_comment_row(video_id, top, is_reply=False))

                total_replies = int(thread_snippet.get("totalReplyCount", 0) or 0)
                replies = item.get("replies", {}).get("comments", [])
                if total_replies > len(replies):
                    # 同梱は最大5件。全返信を取り直す（同梱分は重複するので置き換え）
                    replies, extra = fetch_all_replies(youtube, item.get("id", top.get("id", "")))
                    pages += extra
                for reply in replies:
                    rows.append(_comment_row(video_id, reply, is_reply=True))
    except HttpError as error:
        if _is_comments_disabled(error):
            return [], pages, True
        raise
    return rows, pages, False


def run(
    youtube,
    channel_id: str,
    data_dir: Path,
    force: bool = False,
    max_videos: int = 0,
    coverage_scope: str = common.COMMENT_COVERAGE_OWNER,
) -> dict:
    if coverage_scope not in {
        common.COMMENT_COVERAGE_OWNER,
        common.COMMENT_COVERAGE_PUBLIC,
    }:
        raise ValueError(f"未対応のコメント収集範囲です: {coverage_scope}")
    started_at = common.utcnow()
    playlist_id = get_uploads_playlist(youtube, channel_id)
    videos, pages = list_uploaded_videos(youtube, playlist_id)
    pages += 1  # channels.list の分

    cache_dir = common.comments_dir(data_dir)
    state_file = common.comment_state_path(data_dir)
    common.atomic_write_json(
        {
            "schema_version": 1,
            "status": "in_progress",
            "channel_id": channel_id,
            "coverage_scope": coverage_scope,
            "started_at": common.format_ts(started_at),
            "videos_listed": len(videos),
        },
        state_file,
    )
    collected = skipped = disabled = total_comments = 0
    for i, video in enumerate(videos, start=1):
        video_id = video["video_id"]
        cache_file = cache_dir / f"{video_id}.csv"
        if cache_file.exists() and not force:
            skipped += 1
            continue
        # 上限はキャッシュ済みを除いた「新規収集数」に対して適用する。
        # （全体リストを先頭で切ると、2回目以降の実行がキャッシュ済みの
        #   同じ N 本だけを見て終わり、収集が先へ進まなくなるため）
        if max_videos > 0 and collected >= max_videos:
            print(
                f"--max-videos {max_videos} に到達したため中断します。"
                "再実行すると続きから収集します。"
            )
            break
        rows, video_pages, is_disabled = fetch_video_comments(youtube, video_id)
        pages += video_pages
        frame = pd.DataFrame(rows, columns=COMMENT_COLUMNS)
        if not frame.empty:
            frame = frame.drop_duplicates("comment_id")
        common.atomic_write_csv(frame, cache_file)
        collected += 1
        disabled += 1 if is_disabled else 0
        total_comments += len(frame)
        status = "コメント無効" if is_disabled else f"{len(frame)} 件"
        print(f"[{i}/{len(videos)}] {video_id}: {status} ({video['title'][:40]})")

    cached = sum(
        1 for video in videos if (cache_dir / f"{video['video_id']}.csv").exists()
    )
    missing = len(videos) - cached
    all_refreshed = collected == len(videos)
    completed_at = common.utcnow()
    common.atomic_write_json(
        {
            "schema_version": 1,
            "status": "complete" if missing == 0 else "partial",
            "channel_id": channel_id,
            "coverage_scope": coverage_scope,
            "started_at": common.format_ts(started_at),
            "completed_at": common.format_ts(completed_at),
            "videos_listed": len(videos),
            "videos_cached": cached,
            "videos_missing": missing,
            "videos_refreshed_this_run": collected,
            "all_videos_refreshed_this_run": all_refreshed,
            "comment_coverage_complete": missing == 0,
        },
        state_file,
    )

    return {
        "videos": len(videos),
        "collected": collected,
        "skipped": skipped,
        "disabled": disabled,
        "comments": total_comments,
        "pages": pages,
        "videos_cached": cached,
        "videos_missing": missing,
        "comment_coverage_complete": missing == 0,
        "all_videos_refreshed_this_run": all_refreshed,
        "state_file": state_file,
    }


def build_client(use_oauth: bool, api_key: str | None, data_dir: Path):
    """認証クライアントを解決する（モジュール docstring の優先順位に従う）。"""
    token_file = common.token_path(data_dir)
    if resolve_coverage_scope(use_oauth, api_key, data_dir) == common.COMMENT_COVERAGE_OWNER:
        print("OAuth 認証で収集します（オーナー権限のため非公開・限定公開の動画も対象）")
        return common.build_oauth_client(token_file=token_file)
    print(
        "APIキーで収集します（公開動画のみ対象。非公開・限定公開の動画も含める場合は"
        "先に collect_subscribers.py を実行するか --use-oauth を指定）"
    )
    return common.build_api_key_client(api_key)


def resolve_coverage_scope(use_oauth: bool, api_key: str | None, data_dir: Path) -> str:
    """選択される認証方式から、コメント収集がカバーする動画範囲を返す。"""
    token_file = common.token_path(data_dir)
    oauth_status = common.oauth_config_status(token_file=token_file)
    if use_oauth or (not api_key and oauth_status["noninteractive_oauth_ready"]):
        return common.COMMENT_COVERAGE_OWNER
    return common.COMMENT_COVERAGE_PUBLIC


def resolve_channel_id(args_channel_id: str | None, data_dir: Path) -> str:
    if args_channel_id:
        return args_channel_id
    env_value = os.environ.get("YOUTUBE_CHANNEL_ID", "")
    if env_value:
        return env_value
    saved = common.channel_id_path(data_dir)
    if saved.exists():
        return saved.read_text(encoding="utf-8").strip()
    raise SystemExit(
        "チャンネルIDが未指定です。--channel-id UCxxxx か環境変数 YOUTUBE_CHANNEL_ID を指定するか、"
        "先に collect_subscribers.py を実行してください（自チャンネルIDが保存されます）。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="自チャンネル全動画のコメント収集（返信込み）")
    parser.add_argument("--channel-id", help="対象チャンネルID（UC...）")
    parser.add_argument("--api-key", help="YouTube Data API キー（既定: 環境変数 YOUTUBE_API_KEY）")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=common.DATA_DIR,
        help=f"データ保存先（既定: {common.DATA_DIR}）",
    )
    parser.add_argument("--force", action="store_true", help="キャッシュ済みの動画も再取得する")
    parser.add_argument(
        "--use-oauth",
        action="store_true",
        help="OAuth 認証で収集する（非公開・限定公開の動画も対象。トークンが無ければブラウザ認証）",
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=0,
        help="1回の実行で新規に収集する動画数の上限（0=無制限。キャッシュ済みは数えない。"
        "クォータを複数日に分割したい場合に使用）",
    )
    args = parser.parse_args()

    channel_id = resolve_channel_id(args.channel_id, args.data_dir)
    coverage_scope = resolve_coverage_scope(args.use_oauth, args.api_key, args.data_dir)
    youtube = build_client(args.use_oauth, args.api_key, args.data_dir)
    stats = run(
        youtube,
        channel_id,
        args.data_dir,
        force=args.force,
        max_videos=args.max_videos,
        coverage_scope=coverage_scope,
    )

    print(
        f"動画 {stats['videos']} 本: 取得 {stats['collected']}（うちコメント無効 {stats['disabled']}）/ "
        f"キャッシュ済みスキップ {stats['skipped']} / 収集コメント {stats['comments']} 件 / "
        f"クォータ消費 約{stats['pages']} unit"
    )
    print(
        f"収集状態: キャッシュ {stats['videos_cached']}/{stats['videos']} 本 / "
        f"未取得 {stats['videos_missing']} 本 → {stats['state_file']}"
    )
    if not stats["comment_coverage_complete"]:
        print(
            "注意: 未取得動画があるため「一度もコメントなし」抽出は停止されます。"
            "同じ --max-videos で再実行すると続きから収集します。"
        )
    elif not stats["all_videos_refreshed_this_run"]:
        print(
            "全動画のカバレッジは揃っていますが、既存キャッシュも含みます。"
            "最新状態へ揃える場合は --force で全動画を再取得してください。"
        )
    if stats["skipped"] and not stats["collected"]:
        print("すべてキャッシュ済みです。再取得する場合は --force を付けてください。")


if __name__ == "__main__":
    main()

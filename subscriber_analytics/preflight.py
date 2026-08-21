"""サイレント登録者取得前の安全な設定診断。

秘密値は表示せず、設定元とオンライン権限だけを確認する。--online は既定で既存の
OAuth トークンのみを使う。初回だけ --authorize を併用するとブラウザ認証する
（オンライン診断は約2 quota units）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def _configured(value: bool) -> str:
    return "設定済み" if value else "未設定"


def expected_channel_id(explicit: str | None, data_dir: Path) -> tuple[str, str]:
    if explicit:
        return explicit, "--expected-channel-id"
    common.load_environment()
    env_value = os.environ.get("YOUTUBE_CHANNEL_ID", "").strip()
    if env_value:
        return env_value, "YOUTUBE_CHANNEL_ID"
    saved = common.channel_id_path(data_dir)
    if saved.exists():
        return saved.read_text(encoding="utf-8").strip(), str(saved)
    return "", ""


def error_reason(error) -> str:
    details = getattr(error, "error_details", None) or []
    for detail in details:
        if isinstance(detail, dict) and detail.get("reason"):
            return str(detail["reason"])
    return f"HTTP {getattr(error.resp, 'status', 'unknown')}"


def run_online(
    data_dir: Path,
    client_secret: Path,
    expected: str,
    allow_interactive: bool = False,
) -> None:
    from googleapiclient.errors import HttpError

    youtube = common.build_oauth_client(
        client_secret=client_secret,
        token_file=common.token_path(data_dir),
        allow_interactive=allow_interactive,
    )
    response = youtube.channels().list(part="id,snippet", mine=True).execute()
    items = response.get("items", [])
    if not items:
        raise SystemExit(
            "OAuth ユーザーに API から操作可能な YouTube チャンネルがありません。"
            "YouTube Studio の委任権限は YouTube API では利用できません。"
        )
    channel = items[0]
    actual = channel.get("id", "")
    title = channel.get("snippet", {}).get("title", "")
    print(f"OAuth チャンネル: OK（{title} / {actual}）")
    if expected and actual != expected:
        raise SystemExit(
            f"対象チャンネル不一致: expected={expected}, OAuth={actual}。"
            "この状態では登録者取得を実行しません。"
        )
    if expected:
        print("対象チャンネル照合: OK")
    else:
        print("対象チャンネル照合: 未設定（YOUTUBE_CHANNEL_ID の設定を推奨）")

    try:
        probe = youtube.subscriptions().list(
            part="subscriberSnippet",
            mySubscribers=True,
            maxResults=1,
        ).execute()
    except HttpError as exc:
        raise SystemExit(
            "公開登録者 API の権限確認に失敗しました: " + error_reason(exc)
        ) from exc
    visible = probe.get("pageInfo", {}).get("totalResults", "unknown")
    print(f"subscriptions.list(mySubscribers=true): OK（API表示件数 {visible}）")
    print("オンライン事前診断: 合格（推定クォータ消費 2 units）")


def main() -> None:
    parser = argparse.ArgumentParser(description="subscriber_analytics の取得前設定診断")
    parser.add_argument("--online", action="store_true", help="既存OAuthトークンでAPI権限も検証する")
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="--online 時にトークンが無ければブラウザで初回OAuth認可する（ローカル実行向け）",
    )
    parser.add_argument("--expected-channel-id", help="OAuth先と一致すべきチャンネルID（UC...）")
    parser.add_argument(
        "--client-secret",
        type=Path,
        default=common.DEFAULT_CLIENT_SECRET,
        help=f"OAuthクライアントJSON（既定: {common.DEFAULT_CLIENT_SECRET}）",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=common.DATA_DIR,
        help=f"データ保存先（既定: {common.DATA_DIR}）",
    )
    args = parser.parse_args()

    status = common.oauth_config_status(args.client_secret, common.token_path(args.data_dir))
    expected, expected_source = expected_channel_id(args.expected_channel_id, args.data_dir)
    print("=== subscriber_analytics 取得前診断（秘密値は非表示） ===")
    print(
        f"YouTube API key: {_configured(status['api_key_present'])}"
        "（コメント公開範囲用・OAuthの代替には不可）"
    )
    print(
        f"OAuth client: {_configured(bool(status['client_source']))}"
        + (f" / {status['client_source']}" if status["client_source"] else "")
    )
    print(
        f"OAuth token: {_configured(bool(status['token_source']))}"
        + (f" / {status['token_source']}" if status["token_source"] else "")
    )
    print(
        f"対象 channel ID: {_configured(bool(expected))}"
        + (f" / {expected_source}" if expected_source else "")
    )
    runtime = "実行中" if os.environ.get("CODESPACES") == "true" else "ローカル環境"
    print(f"GitHub Codespaces: {runtime}")

    can_authorize = args.online and args.authorize and status["interactive_auth_possible"]
    if not status["noninteractive_oauth_ready"] and not can_authorize:
        print(
            "判定: 登録者取得は未準備です。OAuth token が必要です。"
            "初回認可用の OAuth client だけでは非対話実行できません。"
        )
        raise SystemExit(2)
    if not expected:
        print("判定: YOUTUBE_CHANNEL_ID を設定して誤アカウント取得を防止してください。")
        raise SystemExit(2)
    if args.online:
        run_online(
            args.data_dir,
            args.client_secret,
            expected,
            allow_interactive=args.authorize,
        )
    else:
        print("ローカル設定診断: 合格。最終確認は --online で実行してください。")


if __name__ == "__main__":
    main()

"""サイレント登録者分析ツールの共通処理。

パス定義・原子的書き込み・期間パース・YouTube API クライアント生成を集約する。
データはすべて subscriber_analytics/ 配下で完結し、リポジトリ既存のファイルには
依存しない（パスはこのファイルからの相対で解決するため、どこから実行してもよい）。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_CLIENT_SECRET = BASE_DIR / "client_secret.json"

OAUTH_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


def snapshot_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "snapshots"


def comments_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "comments"


def registry_path(data_dir: Path) -> Path:
    return Path(data_dir) / "subscriber_registry.csv"


def token_path(data_dir: Path) -> Path:
    return Path(data_dir) / "token.json"


def channel_id_path(data_dir: Path) -> Path:
    return Path(data_dir) / "channel_id.txt"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TIME_FMT)


def parse_ts(text: str) -> datetime:
    dt = pd.to_datetime(text, utc=True, format="ISO8601")
    return dt.to_pydatetime()


_DURATION_RE = re.compile(r"^(\d+)\s*([dwm])$", re.IGNORECASE)


def parse_duration(text: str) -> timedelta:
    """`90d` / `12w` / `3m` 形式の期間指定をパースする（m は30日換算の近似）。"""
    m = _DURATION_RE.match(text.strip())
    if not m:
        raise ValueError(
            f"期間指定 '{text}' を解釈できません。90d / 12w / 3m の形式で指定してください。"
        )
    value = int(m.group(1))
    unit = m.group(2).lower()
    days = {"d": 1, "w": 7, "m": 30}[unit]
    return timedelta(days=value * days)


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    """一時ファイルに書いてから os.replace で置換する（中断しても壊れたCSVを残さない）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def atomic_write_text(text: str, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def iter_pages(resource, request):
    """list / list_next のページネーションを吸収してレスポンスを順に返す。"""
    while request is not None:
        response = request.execute()
        yield response
        request = resource.list_next(request, response)


def build_api_key_client(api_key: str | None = None):
    """APIキー（環境変数 YOUTUBE_API_KEY）による読み取り専用クライアントを返す。"""
    from googleapiclient.discovery import build

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    key = api_key or os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise SystemExit(
            "YOUTUBE_API_KEY が見つかりません。環境変数か .env、または --api-key で指定してください。"
        )
    return build("youtube", "v3", developerKey=key, cache_discovery=False)


def build_oauth_client(
    client_secret: Path = DEFAULT_CLIENT_SECRET,
    token_file: Path | None = None,
):
    """OAuth（youtube.readonly）で認証済みクライアントを返す。

    トークンは token_file にキャッシュし、期限切れならリフレッシュを試みる。
    OAuth 同意画面がテストステータスの場合リフレッシュトークンは7日で失効する
    ため、その場合は自動でブラウザ再認証にフォールバックする。
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_file = Path(token_file) if token_file else token_path(DATA_DIR)
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), OAUTH_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            print(f"トークンの更新に失敗したため再認証します: {exc}")
            creds = None
    if not creds or not creds.valid:
        client_secret = Path(client_secret)
        if not client_secret.exists():
            raise SystemExit(
                f"OAuth クライアントシークレットが見つかりません: {client_secret}\n"
                "Google Cloud Console で OAuth クライアントID（デスクトップアプリ）を作成し、"
                "JSON をこのパスに保存してください（詳細は subscriber_analytics/README.md）。"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), OAUTH_SCOPES)
        creds = flow.run_local_server(port=0)
        atomic_write_text(creds.to_json(), token_file)
        print(f"トークンを保存しました: {token_file}")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)

"""サイレント登録者分析ツールの共通処理。

パス定義・原子的書き込み・期間パース・YouTube API クライアント生成を集約する。
データはすべて subscriber_analytics/ 配下で完結し、リポジトリ既存のファイルには
依存しない（パスはこのファイルからの相対で解決するため、どこから実行してもよい）。
"""

from __future__ import annotations

import json
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
OAUTH_CLIENT_SECRET_ENV = "YOUTUBE_OAUTH_CLIENT_SECRET_JSON"
OAUTH_TOKEN_ENV = "YOUTUBE_OAUTH_TOKEN_JSON"
COMMENT_COVERAGE_OWNER = "owner"
COMMENT_COVERAGE_PUBLIC = "public"
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


def comment_state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "comment_collection_state.json"


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


def atomic_write_json(value: dict, path: Path) -> None:
    atomic_write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        path,
    )


def load_environment() -> None:
    """ローカルの .env を読み込む（既存の環境変数は上書きしない）。"""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def _json_from_env(name: str) -> dict | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{name} が有効な JSON ではありません: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{name} は JSON オブジェクトである必要があります。")
    return value


def oauth_config_status(
    client_secret: Path = DEFAULT_CLIENT_SECRET,
    token_file: Path | None = None,
) -> dict:
    """秘密値を表示せず、OAuth 入力がどこから供給されるかを返す。"""
    load_environment()
    token_file = Path(token_file) if token_file else token_path(DATA_DIR)
    client_secret = Path(client_secret)
    client_source = (
        str(client_secret)
        if client_secret.exists()
        else OAUTH_CLIENT_SECRET_ENV
        if os.environ.get(OAUTH_CLIENT_SECRET_ENV)
        else ""
    )
    token_source = (
        str(token_file)
        if token_file.exists()
        else OAUTH_TOKEN_ENV
        if os.environ.get(OAUTH_TOKEN_ENV)
        else ""
    )
    return {
        "client_source": client_source,
        "token_source": token_source,
        "api_key_present": bool(os.environ.get("YOUTUBE_API_KEY")),
        "interactive_auth_possible": bool(client_source),
        "noninteractive_oauth_ready": bool(token_source),
    }


def iter_pages(resource, request):
    """list / list_next のページネーションを吸収してレスポンスを順に返す。"""
    while request is not None:
        response = request.execute()
        yield response
        request = resource.list_next(request, response)


def build_api_key_client(api_key: str | None = None):
    """APIキー（環境変数 YOUTUBE_API_KEY）による読み取り専用クライアントを返す。"""
    from googleapiclient.discovery import build

    load_environment()

    key = api_key or os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise SystemExit(
            "YOUTUBE_API_KEY が見つかりません。環境変数か .env、または --api-key で指定してください。"
        )
    return build("youtube", "v3", developerKey=key, cache_discovery=False)


def build_oauth_client(
    client_secret: Path = DEFAULT_CLIENT_SECRET,
    token_file: Path | None = None,
    allow_interactive: bool = True,
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

    load_environment()
    token_file = Path(token_file) if token_file else token_path(DATA_DIR)
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), OAUTH_SCOPES)
    else:
        token_info = _json_from_env(OAUTH_TOKEN_ENV)
        if token_info:
            creds = Credentials.from_authorized_user_info(token_info, OAUTH_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            print(f"トークンの更新に失敗したため再認証します: {exc}")
            creds = None
    if not creds or not creds.valid:
        if not allow_interactive:
            raise SystemExit(
                "有効な OAuth トークンがありません。ローカルで一度認可するか、"
                f"GitHub Codespaces secret {OAUTH_TOKEN_ENV} を設定してください。"
            )
        if os.environ.get("CODESPACES") == "true":
            raise SystemExit(
                "Codespaces では localhost の対話OAuthを開始しません。ローカルで "
                "preflight.py --online --authorize を実行し、生成した token.json で "
                f"Codespaces secret {OAUTH_TOKEN_ENV} を更新してください。"
            )
        client_secret = Path(client_secret)
        client_config = None
        if client_secret.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), OAUTH_SCOPES)
        else:
            client_config = _json_from_env(OAUTH_CLIENT_SECRET_ENV)
            if client_config:
                flow = InstalledAppFlow.from_client_config(client_config, OAUTH_SCOPES)
            else:
                raise SystemExit(
                    f"OAuth クライアントシークレットが見つかりません: {client_secret}\n"
                    "Google Cloud Console で OAuth クライアントID（デスクトップアプリ）を作成し、"
                    f"JSON をこのパスに保存するか、{OAUTH_CLIENT_SECRET_ENV} に設定してください"
                    "（詳細は subscriber_analytics/README.md）。"
                )
        creds = flow.run_local_server(port=0)
    # 環境変数から読んだ場合も、更新済みアクセストークンを ignored data/ に保存する。
    atomic_write_text(creds.to_json(), token_file)
    print(f"OAuth トークンを保存/更新しました: {token_file}")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)

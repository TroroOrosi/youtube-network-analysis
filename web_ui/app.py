"""Reference hosted application: server-rendered, Japanese, non-engineer first.

Routes are thin. Every decision about permissions, credentials, collection, and
analysis stays in the domain modules; this layer authenticates the session,
resolves a workspace context, renders safe values, and never sees a credential.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator

from analysis_api.errors import AnalysisApiError
from analysis_api.models import (
    AnalysisFilterInput,
    AnalysisPageRequest,
    ExportAnalysis,
    RunAnalysis,
)
from channel_connections.errors import ChannelConnectionsError
from channel_connections.models import (
    BeginAuthorization,
    BeginReauthorization,
    CompleteAuthorization,
    DisconnectConnection,
    RedactedSecret,
)
from channel_data.errors import ChannelDataError
from collection_jobs.errors import CollectionJobsError
from collection_jobs.models import (
    CollectionRun,
    EnqueueRun,
    JobsPageRequest,
    RunKind,
)
from workspace_access.models import (
    AccessSecret,
    CreateWorkspace,
    Permission,
    SessionEvidence,
    VerifiedIdentity,
    WorkspaceContext,
    WorkspaceSelection,
)
from workspace_access.errors import WorkspaceAccessError

from .container import Services, build_services
from .google_login import STATE_TTL, GoogleLogin, LoginFailed

SESSION_COOKIE = "yna_session"
WORKSPACE_COOKIE = "yna_workspace"
CSRF_COOKIE = "yna_csrf"
LOGIN_COOKIE = "yna_login"

CONSENT_ORIGIN = "https://accounts.google.com"
WRITE_LIMIT_PER_MINUTE = 30
COLLECT_LIMIT_PER_MINUTE = 3
COLLECTION_WRITE_PATHS = frozenset({"/collecting/step"})
# How long one call may spend collecting. The browser's slice is short
# because a person is watching a page that is not answering yet; the
# scheduler's is long because nobody is, and the only cost of a longer one
# is request time we are already paying for.
BROWSER_SLICE_SECONDS = 20
DRAIN_SLICE_SECONDS = 120
# How many pages of runs one view will read before it stops asking. Fifty
# thousand runs is far more history than any page needs, and the cap is what
# keeps one request from walking a whole workspace's past.
RUN_PAGE_CAP = 20
RUN_PAGE_SIZE = 100
RATE_WINDOW = timedelta(minutes=1)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#0b5cab"/>
<path d="M17 42 29 30l8 7 11-15" fill="none" stroke="#fff" stroke-width="6"
 stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="17" cy="42" r="4" fill="#fff"/><circle cx="29" cy="30" r="4" fill="#fff"/>
<circle cx="37" cy="37" r="4" fill="#fff"/><circle cx="48" cy="22" r="4" fill="#fff"/>
</svg>"""

MESSAGES = {
    "connected": "チャンネルを接続しました。",
    "disconnected": "接続を解除しました。収集済みデータは残っています。",
    "collected": "データ収集が完了しました。",
    "collect_partial": "本日の取得上限に達したため、途中まで収集しました。明日以降に再実行してください。",
    "collect_failed": "収集できませんでした。接続の再認可が必要な可能性があります。",
    "collect_suspended": (
        "本日の取得上限に達しました。"
        "続きは翌日以降に自動で再開します。このまま閉じて構いません。"
    ),
    "collect_stopped": "収集を中断しました。次に開いたときに続きから再開します。",
    "workspace_created": "ワークスペースを作成しました。",
    "authorization_cancelled": "認可を中止しました。",
}

ERROR_TEXT = {
    "PERMISSION_DENIED": ("この操作を行う権限がありません。", "ワークスペースの管理者に権限を依頼してください。"),
    "CONNECTION_NOT_FOUND_OR_FORBIDDEN": ("その接続は見つかりません。", "一覧から選び直してください。"),
    "CONNECTION_ALREADY_EXISTS": ("そのチャンネルはすでに接続済みです。", "一覧の接続を利用してください。"),
    "INTENT_NOT_FOUND_OR_EXPIRED": ("認可の有効期限が切れました。", "もう一度「接続する」から始めてください。"),
    "CALLBACK_CONFLICT": ("認可の情報が一致しませんでした。", "もう一度最初から接続してください。"),
    "CONNECTION_REAUTH_REQUIRED": ("接続の再認可が必要です。", "「再認可する」を実行してください。"),
    "TOO_MANY_REQUESTS": ("操作が多すぎます。", "しばらく待ってからもう一度お試しください。"),
    "PROVIDER_AUTHORIZATION_FAILED": ("YouTube 側の認可を完了できませんでした。", "しばらく待ってから再試行してください。"),
    "PROVIDER_CAPABILITY_MISSING": ("このチャンネルでは登録者情報を取得できません。", "チャンネル所有者の Google アカウントで認可してください。"),
    "RUN_ALREADY_ACTIVE": ("同じ種類の収集がすでに実行中です。", "完了を待ってから再実行してください。"),
    "DATASET_NOT_READY": ("分析できるデータがまだありません。", "先にデータ収集を実行してください。"),
    "VIEW_NOT_FOUND_OR_FORBIDDEN": ("その保存条件は見つかりません。", "一覧から選び直してください。"),
    "INVALID_CURSOR": ("ページの位置が無効になりました。", "1 ページ目から表示し直してください。"),
    "CURSOR_EXPIRED": ("データが更新されたためページを表示できません。", "最新の結果を読み込み直してください。"),
    "INVALID_INPUT": ("入力内容を確認してください。", "値を修正して再度お試しください。"),
    "LOGIN_FAILED": ("ログインを完了できませんでした。", "ログイン画面からもう一度お試しください。"),
    "LOGIN_METHOD_UNAVAILABLE": (
        "このログイン方法は利用できません。",
        "ログイン画面に表示される方法でログインしてください。",
    ),
    "WORKSPACE_NOT_FOUND_OR_FORBIDDEN": (
        "そのワークスペースは利用できません。",
        "ホームからワークスペースを選び直してください。",
    ),
    "NO_ACCESSIBLE_WORKSPACE": (
        "利用できるワークスペースがありません。",
        "新しいワークスペースを作成してください。",
    ),
    "CSRF": (
        "この操作を完了できませんでした。",
        "ページを開き直してから、もう一度実行してください。",
    ),
    "WORKSPACE_SELECTION_REQUIRED": (
        "ワークスペースを選択してください。",
        "ホームから対象のワークスペースを選んでください。",
    ),
}

READINESS_TEXT = {
    "NO_SUBSCRIBER_SNAPSHOT": "登録者データが未収集です。",
    "NO_VIDEO_INVENTORY": "動画一覧が未収集です。",
    "PUBLIC_VIDEO_SCOPE_ONLY": "所有者権限での動画取得が必要です。",
    "COMMENTS_INCOMPLETE": "コメントの収集が完了していません。",
}

SEGMENT_LABELS = {
    "NEW_SILENT": "新規サイレント",
    "OLD_SILENT": "長期サイレント",
    "DORMANT": "休眠",
    "ACTIVE": "アクティブ",
}

STATUS_LABELS = {
    "ACTIVE": "利用可能",
    "REAUTH_REQUIRED": "再認可が必要",
}

RUN_KIND_LABELS = {
    "SUBSCRIBERS": "登録者",
    "OWNER_CONTENT": "動画とコメント",
}

RUN_STATUS_LABELS = {
    "QUEUED": "待機中",
    "RUNNING": "実行中",
    "SUCCEEDED": "完了",
    "PARTIAL": "一部のみ完了",
    "FAILED": "失敗",
    "CANCELLED": "中止",
}

RUN_FAILURE_LABELS = {
    "QUOTA_EXHAUSTED": "本日の取得上限",
    "PROVIDER_UNAVAILABLE": "YouTubeの一時的なエラー",
    "REAUTH_REQUIRED": "再認可が必要",
    "CANCELLED": "利用者が中断",
    "UNEXPECTED_FAILURE": "予期しないエラー",
}

JST = timezone(timedelta(hours=9), "JST")


def _display_datetime(value: datetime) -> tuple[str, str]:
    local = value.astimezone(JST)
    return local.isoformat(), local.strftime("%Y/%m/%d %H:%M")


class AppError(Exception):
    def __init__(self, code: str, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class _RateLimiter:
    """Fixed window counters for one process.

    A multi-instance deployment needs a shared store; this only protects the
    instance it runs in, and it is deliberately not the provider quota guard,
    which `collection-jobs` already owns.
    """

    def __init__(self) -> None:
        self._windows: dict[str, tuple[datetime, int]] = {}
        self._lock = Lock()

    def allow(self, key: str, limit: int) -> bool:
        now = datetime.now(UTC)
        with self._lock:
            started, used = self._windows.get(key, (now, 0))
            if now - started >= RATE_WINDOW:
                started, used = now, 0
            if used >= limit:
                return False
            self._windows[key] = (started, used + 1)
            return True


def _is_collection_write(path: str) -> bool:
    return path in COLLECTION_WRITE_PATHS or (
        path.startswith("/connections/") and path.endswith("/collect")
    )


def _client_key(request: Request) -> str:
    """Who to count a request against, behind Cloud Run's front end.

    Every request arrives from the same proxy, so `request.client.host` is one
    address for the whole internet: one visitor hitting the limit would lock
    out everybody. The front end appends the address it accepted the connection
    from to `X-Forwarded-For`, after anything the caller sent, so the last
    element is the one entry a caller cannot choose for itself. Earlier
    elements are caller-supplied and are ignored on purpose.
    """

    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if hops:
        return hops[-1]
    return request.client.host if request.client else "unknown"


def _purge_aged_out(services: Services, context: WorkspaceContext) -> None:
    """Drop what the retention rules already say is over.

    `channel-data` keeps subscriber snapshots for a year and collection records
    for ninety days, and `channel-connections` keeps a consent intent only
    until it expires. Those cutoffs are enforced inside `purge_retention`, and
    nothing was calling it. Each module keeps all of its state in one stored
    document with a hard size limit, so what is never dropped is what
    eventually stops every write. A collection that has just finished is the
    moment to ask, because it is the moment the workspace grew.

    Only an owner may sweep; for a member it waits for an owner's visit rather
    than failing the page they were looking at.
    """

    if Permission.CHANNEL_MANAGE_CONNECTION not in context.permissions:
        return
    moment = datetime.now(UTC)
    services.channel_data.purge_retention(context, moment)
    services.connections.purge_retention(context, moment)


def _every_run(services: Services, context: WorkspaceContext) -> tuple[CollectionRun, ...]:
    """Every run of a workspace, not the first page of them.

    Whether the collecting page keeps refreshing is decided by whether anything
    is still queued. A workspace that has collected for a while has more runs
    than one page holds, and reading only the first page would call the work
    finished while a queued run sat on the second. Paging stops at
    `RUN_PAGE_CAP` so a long history cannot turn one page view into an
    unbounded read.
    """

    runs: list[CollectionRun] = []
    cursor: str | None = None
    for _ in range(RUN_PAGE_CAP):
        page = services.jobs.list_runs(
            context, page=JobsPageRequest(cursor=cursor, limit=RUN_PAGE_SIZE)
        )
        runs.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    return tuple(runs)


def _collection_progress(
    services: Services, context: WorkspaceContext, moment: datetime
) -> tuple[tuple[CollectionRun, ...], tuple[CollectionRun, ...], bool]:
    runs = _every_run(services, context)
    waiting = tuple(
        run for run in runs if run.status.value in {"QUEUED", "RUNNING"}
    )
    suspended = bool(waiting) and all(
        run.next_attempt_at is not None and run.next_attempt_at > moment
        for run in waiting
    )
    return runs, waiting, suspended


def create_app(services: Services | None = None, *, base_url: str = "https://localhost") -> FastAPI:
    app = FastAPI(title="YouTube 分析", docs_url=None, redoc_url=None)
    app.state.services = services or build_services(base_url)

    limiter = _RateLimiter()

    @app.middleware("http")
    async def rate_limit(request: Request, call_next: Any) -> Response:
        """Cap writes per client. Reads stay free so a page never breaks."""

        if request.method != "GET":
            limit = (
                COLLECT_LIMIT_PER_MINUTE
                if _is_collection_write(request.url.path)
                else WRITE_LIMIT_PER_MINUTE
            )
            client = _client_key(request)
            if not limiter.allow(f"{client}|{limit}", limit):
                return _error_response(request, "TOO_MANY_REQUESTS", 429)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';"
            f" form-action 'self' {CONSENT_ORIGIN}; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        # Every page is served over TLS by Cloud Run, but the first request
        # of a session can still be a plain http one that carries the
        # cookies before the redirect. A year of HSTS removes that request.
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        # Every page here is a signed-in view of one workspace, or a redirect
        # that depends on one. Stored in a shared cache or replayed by the back
        # button after a sign-out, any of them shows one person's channels to
        # whoever uses the browser next.
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    _register_routes(app)
    return app


# Session and CSRF helpers


def _services(request: Request) -> Services:
    return request.app.state.services


def _session(request: Request):
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return _services(request).access.authenticate_session(
            SessionEvidence(secret=AccessSecret(raw))
        )
    except WorkspaceAccessError:
        return None


def _require_session(request: Request):
    session = _session(request)
    if session is None:
        raise AppError("UNAUTHENTICATED", status_code=401)
    return session


def _check_csrf(request: Request, token: str) -> None:
    expected = request.cookies.get(CSRF_COOKIE)
    if not expected or not secrets.compare_digest(expected, token or ""):
        raise AppError("CSRF", status_code=403)


def _context(request: Request, session, permission: Permission):
    workspace_id = request.cookies.get(WORKSPACE_COOKIE)
    selection = WorkspaceSelection(workspace_id=workspace_id) if workspace_id else None
    return _services(request).access.resolve_workspace_context(
        session, selection, permission
    )


OptionalInt = Annotated[
    int | None, BeforeValidator(lambda value: None if value == "" else value)
]


@dataclass(frozen=True, slots=True)
class _AnalysisFilters:
    never_commented: bool = False
    subscribed_within_days: int | None = None
    segment: str | None = None

    def domain_input(self) -> AnalysisFilterInput:
        return AnalysisFilterInput(
            never_commented=self.never_commented,
            subscribed_within_days=self.subscribed_within_days,
            segments=(self.segment,) if self.segment else (),
        )

    def query_pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        if self.segment:
            pairs.append(("segment", self.segment))
        if self.subscribed_within_days is not None:
            pairs.append(
                ("subscribed_within_days", str(self.subscribed_within_days))
            )
        if self.never_commented:
            pairs.append(("never_commented", "true"))
        return pairs


def _login_provider(request: Request) -> GoogleLogin:
    login = _services(request).login
    if login is None:
        raise AppError("LOGIN_METHOD_UNAVAILABLE", 404)
    return login


def _session_response(request: Request, identity: VerifiedIdentity) -> Response:
    issued = _services(request).access.establish_session(identity)
    response = _redirect("/")
    response.set_cookie(
        SESSION_COOKIE,
        issued.secret.reveal(),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


def _redirect(path: str, message: str | None = None) -> RedirectResponse:
    target = f"{path}?msg={message}" if message else path
    return RedirectResponse(target, status_code=303)


def _render(
    request: Request, template: str, values: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    csrf = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)
    payload = {
        "request": request,
        "csrf_token": csrf,
        "message": MESSAGES.get(request.query_params.get("msg", ""), None),
        **values,
    }
    response = TEMPLATES.TemplateResponse(request, template, payload, status_code=status_code)
    response.set_cookie(
        CSRF_COOKIE, csrf, httponly=True, secure=True, samesite="strict", path="/"
    )
    return response


def _error_response(request: Request, code: str, status_code: int) -> HTMLResponse:
    title, action = ERROR_TEXT.get(
        code, ("処理を完了できませんでした。", "時間をおいて再度お試しください。")
    )
    return _render(
        request,
        "error.html",
        {"error_title": title, "error_action": action, "error_code": code},
        status_code=status_code,
    )


def _collect_outcome(runs: tuple[CollectionRun, ...]) -> str:
    """Name what the browser should be told about a collection that ended.

    The worst status wins, because a person reading one line wants to know
    whether to act, and a run that failed is the one that needs them.
    """

    statuses = {run.status.value for run in runs}
    if statuses & {"FAILED", "CANCELLED"}:
        return "collect_failed"
    if "PARTIAL" in statuses:
        return "collect_partial"
    return "collected"


def _domain_error(error: Exception) -> AppError:
    code = getattr(error, "code", "INVALID_INPUT")
    status = {
        "PERMISSION_DENIED": 403,
        "CONNECTION_NOT_FOUND_OR_FORBIDDEN": 404,
        "VIEW_NOT_FOUND_OR_FORBIDDEN": 404,
        "RUN_NOT_FOUND_OR_FORBIDDEN": 404,
        "DATASET_NOT_READY": 409,
        "CONNECTION_ALREADY_EXISTS": 409,
        "RUN_ALREADY_ACTIVE": 409,
        "CONNECTION_REAUTH_REQUIRED": 409,
        "WORKSPACE_NOT_FOUND_OR_FORBIDDEN": 404,
        "MEMBERSHIP_NOT_FOUND_OR_FORBIDDEN": 404,
        "SESSION_EXPIRED_OR_REVOKED": 401,
        "UNAUTHENTICATED": 401,
    }.get(code, 400)
    return AppError(code, status_code=status)


def _register_routes(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, error: AppError) -> Response:
        if error.code == "UNAUTHENTICATED":
            return _redirect("/login")
        return _error_response(request, error.code, error.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, error: RequestValidationError
    ) -> Response:
        return _error_response(request, "INVALID_INPUT", 400)

    @app.exception_handler(WorkspaceAccessError)
    @app.exception_handler(ChannelConnectionsError)
    @app.exception_handler(ChannelDataError)
    @app.exception_handler(CollectionJobsError)
    @app.exception_handler(AnalysisApiError)
    async def handle_domain_error(request: Request, error: Exception) -> Response:
        mapped = _domain_error(error)
        return _error_response(request, mapped.code, mapped.status_code)

    # The routes below are deliberately not `async def`. Every one of them ends
    # up in a blocking call: the state stores are Firestore over HTTP, the
    # provider adapters are the YouTube Data API, and none of that is written
    # against an event loop. Declared `async`, each of those waits held the one
    # loop this process has, so a slow Google call stalled every other request
    # in flight, including the ones that touch nothing. Declared as ordinary
    # functions, Starlette runs them in its thread pool and the waits overlap.
    #
    # The price is that handlers now run concurrently in threads, which is why
    # every service this reaches guards its own state with a lock — including
    # `GoogleCredentialStore`, whose vault would otherwise interleave two saves.
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        return _render(
            request, "login.html", {"google_login": _services(request).login is not None}
        )

    @app.post("/login")
    def login(
        request: Request, display_name: str = Form(...), csrf_token: str = Form("")
    ) -> Response:
        """Open a session from a typed name, only where no real provider exists.

        A deployment with a registered client must not keep this door: it would
        let anyone claim any identity the provider is meanwhile verifying.
        """

        _check_csrf(request, csrf_token)
        if _services(request).login is not None:
            raise AppError("LOGIN_METHOD_UNAVAILABLE", 404)
        subject = display_name.strip()
        if not subject or len(subject) > 80:
            raise AppError("INVALID_INPUT")
        return _session_response(
            request,
            VerifiedIdentity(
                issuer="urn:demo:local",
                subject=subject,
                authenticated_at=datetime.now(UTC),
                display_name=subject,
            ),
        )

    @app.post("/login/google")
    def login_with_google(
        request: Request, csrf_token: str = Form("")
    ) -> Response:
        _check_csrf(request, csrf_token)
        url, state = _login_provider(request).start()
        response = _redirect(url)
        response.set_cookie(
            LOGIN_COOKIE,
            state,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=int(STATE_TTL.total_seconds()),
            path="/",
        )
        return response

    @app.get("/login/callback")
    def login_return(
        request: Request,
        state: str = Query(...),
        code: str | None = Query(None),
        error: str | None = Query(None),
    ) -> Response:
        """Google's redirect back. The state must match this browser too.

        The provider proves who signed in, not whose browser asked. A state
        kept only in this process would let somebody finish their own
        sign-in inside your browser and leave you working in their
        workspace, so `start` also wrote the state as a cookie and it has
        to come back with it.
        """

        started = request.cookies.get(LOGIN_COOKIE) or ""
        if not secrets.compare_digest(started, state):
            raise AppError("LOGIN_FAILED")
        try:
            identity = _login_provider(request).complete(
                state=state, code=code, error=error
            )
        except LoginFailed as failure:
            raise AppError("LOGIN_FAILED") from failure
        response = _session_response(request, identity)
        response.delete_cookie(LOGIN_COOKIE, path="/")
        return response

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form("")) -> Response:
        _check_csrf(request, csrf_token)
        raw = request.cookies.get(SESSION_COOKIE)
        if raw:
            try:
                _services(request).access.logout(
                    SessionEvidence(secret=AccessSecret(raw))
                )
            except WorkspaceAccessError:
                pass
        response = _redirect("/login")
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(WORKSPACE_COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        session = _require_session(request)
        services = _services(request)
        try:
            workspaces = services.access.list_accessible_workspaces(session)
        except WorkspaceAccessError as error:
            if error.code != "NO_ACCESSIBLE_WORKSPACE":
                raise
            workspaces = ()
        if not workspaces:
            return _render(request, "workspace_new.html", {"session": session})

        context = _context(request, session, Permission.CHANNEL_READ)
        connections = services.connections.list_connections(context)
        runs = services.jobs.list_runs(context)
        connection_titles = {
            item.connection_id: item.channel_title for item in connections.items
        }
        rendered_runs = []
        for item in runs.items[:5]:
            timestamp = item.finished_at or item.started_at or item.enqueued_at
            timestamp_iso, timestamp_label = _display_datetime(timestamp)
            failure = item.failure_reason.value if item.failure_reason else None
            rendered_runs.append(
                {
                    "channel": connection_titles.get(
                        item.connection_id, item.provider_channel_id
                    ),
                    "kind": RUN_KIND_LABELS.get(item.kind.value, item.kind.value),
                    "status": RUN_STATUS_LABELS.get(
                        item.status.value, item.status.value
                    ),
                    "timestamp_iso": timestamp_iso,
                    "timestamp_label": timestamp_label,
                    "detail": RUN_FAILURE_LABELS.get(failure, "") if failure else "—",
                    "quota_spent": item.quota_spent,
                }
            )
        return _render(
            request,
            "dashboard.html",
            {
                "session": session,
                "workspaces": workspaces,
                "current_workspace_id": context.workspace_id,
                "connections": [
                    {
                        "connection_id": item.connection_id,
                        "title": item.channel_title,
                        "channel_id": item.provider_channel_id,
                        "status": item.status.value,
                        "status_label": STATUS_LABELS[item.status.value],
                        "connected_at": item.connected_at,
                    }
                    for item in connections.items
                ],
                "runs": rendered_runs,
            },
        )

    @app.post("/workspaces")
    def create_workspace(
        request: Request, name: str = Form(...), csrf_token: str = Form("")
    ) -> Response:
        _check_csrf(request, csrf_token)
        session = _require_session(request)
        created = _services(request).access.create_workspace(
            session, CreateWorkspace(name=name.strip())
        )
        response = _redirect("/", "workspace_created")
        response.set_cookie(
            WORKSPACE_COOKIE,
            created.workspace_id,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/workspaces/select")
    def select_workspace(
        request: Request, workspace_id: str = Form(...), csrf_token: str = Form("")
    ) -> Response:
        _check_csrf(request, csrf_token)
        session = _require_session(request)
        _services(request).access.resolve_workspace_context(
            session,
            WorkspaceSelection(workspace_id=workspace_id),
            Permission.WORKSPACE_READ,
        )
        response = _redirect("/")
        response.set_cookie(
            WORKSPACE_COOKIE,
            workspace_id,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/connections/start")
    def start_connection(request: Request, csrf_token: str = Form("")) -> Response:
        _check_csrf(request, csrf_token)
        session = _require_session(request)
        context = _context(request, session, Permission.CHANNEL_MANAGE_CONNECTION)
        start = _services(request).connections.begin_authorization(
            context, BeginAuthorization(idempotency_key=secrets.token_urlsafe(16))
        )
        return RedirectResponse(start.authorization_url, status_code=303)

    @app.post("/connections/{connection_id}/reauthorize")
    def reauthorize_connection(
        request: Request, connection_id: str, csrf_token: str = Form("")
    ) -> Response:
        _check_csrf(request, csrf_token)
        session = _require_session(request)
        context = _context(request, session, Permission.CHANNEL_MANAGE_CONNECTION)
        start = _services(request).connections.begin_reauthorization(
            context,
            BeginReauthorization(
                connection_id=connection_id,
                idempotency_key=secrets.token_urlsafe(16),
            ),
        )
        return RedirectResponse(start.authorization_url, status_code=303)

    @app.get("/demo/consent", response_class=HTMLResponse)
    def demo_consent(request: Request, state: str = Query(...)) -> Response:
        return _render(request, "consent.html", {"state": state})

    @app.get("/oauth/callback")
    def oauth_return(
        request: Request,
        state: str = Query(...),
        code: str | None = Query(None),
        error: str | None = Query(None),
    ) -> Response:
        """The provider's own redirect back. `state` is the only accepted proof."""

        session = _require_session(request)
        context = _context(request, session, Permission.CHANNEL_MANAGE_CONNECTION)
        _services(request).connections.complete_authorization(
            context,
            CompleteAuthorization(
                state=RedactedSecret(state),
                code=RedactedSecret(code) if code else None,
                provider_error=None if code else (error or "access_denied"),
                idempotency_key=secrets.token_urlsafe(16),
            ),
        )
        return _redirect("/", "connected")

    @app.post("/oauth/callback")
    def oauth_callback(
        request: Request,
        state: str = Form(...),
        decision: str = Form("approve"),
        csrf_token: str = Form(""),
    ) -> Response:
        _check_csrf(request, csrf_token)
        session = _require_session(request)
        context = _context(request, session, Permission.CHANNEL_MANAGE_CONNECTION)
        if decision != "approve":
            _services(request).connections.complete_authorization(
                context,
                CompleteAuthorization(
                    state=RedactedSecret(state),
                    provider_error="access_denied",
                    idempotency_key=secrets.token_urlsafe(16),
                ),
            )
        _services(request).connections.complete_authorization(
            context,
            CompleteAuthorization(
                state=RedactedSecret(state),
                code=RedactedSecret(secrets.token_urlsafe(24)),
                idempotency_key=secrets.token_urlsafe(16),
            ),
        )
        return _redirect("/", "connected")

    @app.post("/connections/{connection_id}/disconnect")
    def disconnect(
        request: Request, connection_id: str, csrf_token: str = Form("")
    ) -> Response:
        _check_csrf(request, csrf_token)
        session = _require_session(request)
        context = _context(request, session, Permission.CHANNEL_MANAGE_CONNECTION)
        _services(request).connections.disconnect(
            context,
            DisconnectConnection(
                connection_id=connection_id, idempotency_key=secrets.token_urlsafe(16)
            ),
        )
        return _redirect("/", "disconnected")

    @app.post("/connections/{connection_id}/collect")
    def collect(
        request: Request, connection_id: str, csrf_token: str = Form("")
    ) -> Response:
        """Queue the work, then hand the browser the page that does it.

        Nothing is collected here on purpose. A channel's comments can take
        longer than a request may stay open, and past the daily quota they take
        longer than a day, so a route that collected until it was finished would
        either time out or lie about being done. This only enqueues; `/collecting`
        works the queue a slice at a time.
        """

        _check_csrf(request, csrf_token)
        session = _require_session(request)
        context = _context(request, session, Permission.COLLECTION_RUN)
        services = _services(request)
        for kind in (RunKind.SUBSCRIBERS, RunKind.OWNER_CONTENT):
            services.jobs.enqueue_run(
                context,
                EnqueueRun(
                    connection_id=connection_id,
                    kind=kind,
                    idempotency_key=secrets.token_urlsafe(16),
                ),
            )
        return _redirect("/collecting")

    @app.get("/collecting", response_class=HTMLResponse)
    def collecting(request: Request) -> Response:
        """Show progress without changing state or spending provider quota."""

        session = _require_session(request)
        context = _context(request, session, Permission.COLLECTION_RUN)
        services = _services(request)
        runs, waiting, suspended = _collection_progress(
            services, context, datetime.now(UTC)
        )
        if not waiting:
            return _redirect("/")
        if suspended:
            return _redirect("/", "collect_suspended")
        return _render(
            request,
            "collecting.html",
            {
                "session": session,
                "waiting": len(waiting),
                "runs": [
                    {
                        "kind": RUN_KIND_LABELS.get(run.kind.value, run.kind.value),
                        "status": RUN_STATUS_LABELS.get(
                            run.status.value, run.status.value
                        ),
                        "pages_fetched": run.pages_fetched,
                        "quota_spent": run.quota_spent,
                    }
                    for run in runs[:5]
                ],
            },
        )

    @app.post("/collecting/step")
    def collecting_step(request: Request, csrf_token: str = Form("")) -> Response:
        """Run one bounded slice; only a CSRF-protected write may spend quota."""

        _check_csrf(request, csrf_token)
        session = _require_session(request)
        context = _context(request, session, Permission.COLLECTION_RUN)
        services = _services(request)
        worked = services.jobs.execute_due_runs(
            context, datetime.now(UTC), BROWSER_SLICE_SECONDS
        )
        _, waiting, suspended = _collection_progress(
            services, context, datetime.now(UTC)
        )
        if not waiting:
            _purge_aged_out(services, context)
            return _redirect("/", _collect_outcome(worked) if worked else None)
        if suspended:
            return _redirect("/", "collect_suspended")
        return _redirect("/collecting")

    @app.get("/assets/collecting.js", include_in_schema=False)
    def collecting_script() -> Response:
        return Response(
            "document.getElementById('collection-step')?.requestSubmit();\n",
            media_type="application/javascript",
        )

    @app.get("/assets/favicon.svg", include_in_schema=False)
    def favicon() -> Response:
        return Response(FAVICON_SVG, media_type="image/svg+xml")

    @app.post("/internal/drain")
    def drain(request: Request) -> Response:
        """Continue every workspace's queued work, for a caller that is a clock.

        There is no session here, and there must not be one: the runs this
        finishes outlive the evening whoever started them was having, and a job
        that borrowed their session would either die when they signed out or
        keep acting as them after. The caller instead proves it is the scheduler
        this deployment was configured with, and each workspace's work is done
        under authority issued for that workspace alone, carrying the single
        permission collecting needs.

        The answer is a count and nothing else. Which workspaces exist, and
        which of them have work waiting, are not facts this endpoint hands to
        whoever asked.
        """

        caller = _services(request).drain_caller
        if caller is None or not caller.verify(request.headers.get("authorization")):
            # No body, and the same answer whether the deployment has a
            # scheduler at all: a closed door describes itself to nobody.
            return Response(status_code=401)
        services = _services(request)
        deadline = datetime.now(UTC) + timedelta(seconds=DRAIN_SLICE_SECONDS)
        # Keep going until the slice is spent, rather than making one pass over
        # the workspaces that were due at the start. A run that stops because
        # its own slice ran out is due again immediately, and the point of this
        # endpoint is to be the caller that finishes it.
        worked = 0
        purged: set[str] = set()
        while True:
            moment = datetime.now(UTC)
            if int((deadline - moment).total_seconds()) < 1:
                break
            due = services.jobs.due_workspace_ids(moment)
            if not due:
                break
            before = worked
            for workspace_id in due:
                moment = datetime.now(UTC)
                remaining = int((deadline - moment).total_seconds())
                if remaining < 1:
                    break
                context = services.access.issue_job_context(
                    workspace_id, Permission.COLLECTION_RUN
                )
                worked += len(
                    services.jobs.execute_due_runs(context, moment, remaining)
                )
                if workspace_id not in purged:
                    # Runs, quota rows and idempotency records all live in one
                    # stored document with a hard size limit, and their
                    # retention cutoffs only take effect when something asks
                    # for them. This is the only caller that comes round on its
                    # own, so it is the one that has to ask, once per workspace
                    # per call. It needs no permission beyond the one it
                    # already holds for collecting.
                    services.jobs.purge_retention(context, moment)
                    purged.add(workspace_id)
            if worked == before:
                # A pass that moved nothing will not move anything on the next
                # one either: whatever is due cannot be worked right now, and
                # spinning until the deadline would only burn the instance.
                break
        return JSONResponse({"worked": worked})

    @app.get("/analysis", response_class=HTMLResponse)
    def analysis(
        request: Request,
        channel_id: str = Query(...),
        never_commented: bool = Query(False),
        subscribed_within_days: Annotated[OptionalInt, Query()] = None,
        segment: str | None = Query(None),
        cursor: str | None = Query(None),
    ) -> Response:
        session = _require_session(request)
        context = _context(request, session, Permission.ANALYSIS_READ)
        filters = _AnalysisFilters(
            never_commented=never_commented,
            subscribed_within_days=subscribed_within_days,
            segment=segment,
        )
        page = _services(request).analysis.run_analysis(
            context,
            RunAnalysis(channel_id=channel_id, filters=filters.domain_input()),
            AnalysisPageRequest(cursor=cursor, limit=50),
        )
        next_url = None
        if page.next_cursor is not None:
            next_query = [("channel_id", channel_id), *filters.query_pairs()]
            next_query.append(("cursor", page.next_cursor))
            next_url = f"/analysis?{urlencode(next_query)}"
        return _render(
            request,
            "analysis.html",
            {
                "session": session,
                "channel_id": channel_id,
                "summary": page.summary,
                "segment_counts": [
                    (SEGMENT_LABELS.get(name, name), count)
                    for name, count in page.summary.segment_counts
                ],
                "rows": [
                    {
                        "subscriber_channel_id": row.subscriber_channel_id,
                        "title": row.title,
                        "subscribed_at": row.subscribed_at,
                        "segment_label": SEGMENT_LABELS.get(row.segment, row.segment),
                        "comment_count": row.comment_count,
                        "last_comment_at": row.last_comment_at,
                    }
                    for row in page.rows
                ],
                "filters": filters,
                "next_url": next_url,
                "segment_options": list(SEGMENT_LABELS.items()),
            },
        )

    @app.post("/analysis/export")
    def export(
        request: Request,
        channel_id: str = Form(...),
        never_commented: bool = Form(False),
        subscribed_within_days: Annotated[OptionalInt, Form()] = None,
        segment: str | None = Form(None),
        csrf_token: str = Form(""),
    ) -> Response:
        _check_csrf(request, csrf_token)
        session = _require_session(request)
        context = _context(request, session, Permission.ANALYSIS_EXPORT)
        filters = _AnalysisFilters(
            never_commented=never_commented,
            subscribed_within_days=subscribed_within_days,
            segment=segment,
        )
        document = _services(request).analysis.export_analysis(
            context,
            ExportAnalysis(
                channel_id=channel_id,
                filters=filters.domain_input(),
            ),
        )
        return Response(
            content=document.content,
            media_type=document.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{document.filename}"'
            },
        )

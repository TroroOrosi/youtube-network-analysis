"""Reference hosted application: server-rendered, Japanese, non-engineer first.

Routes are thin. Every decision about permissions, credentials, collection, and
analysis stays in the domain modules; this layer authenticates the session,
resolves a workspace context, renders safe values, and never sees a credential.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
    CompleteAuthorization,
    DisconnectConnection,
    RedactedSecret,
)
from channel_data.errors import ChannelDataError
from collection_jobs.errors import CollectionJobsError
from collection_jobs.models import EnqueueRun, ExecuteRun, RunKind
from workspace_access.models import (
    AccessSecret,
    CreateWorkspace,
    Permission,
    SessionEvidence,
    VerifiedIdentity,
    WorkspaceSelection,
)
from workspace_access.errors import WorkspaceAccessError

from .container import Services, build_services

SESSION_COOKIE = "yna_session"
WORKSPACE_COOKIE = "yna_workspace"
CSRF_COOKIE = "yna_csrf"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

MESSAGES = {
    "connected": "チャンネルを接続しました。",
    "disconnected": "接続を解除しました。収集済みデータは残っています。",
    "collected": "データ収集が完了しました。",
    "collect_partial": "本日の取得上限に達したため、途中まで収集しました。明日以降に再実行してください。",
    "collect_failed": "収集できませんでした。接続の再認可が必要な可能性があります。",
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
    "PROVIDER_AUTHORIZATION_FAILED": ("YouTube 側の認可を完了できませんでした。", "しばらく待ってから再試行してください。"),
    "PROVIDER_CAPABILITY_MISSING": ("このチャンネルでは登録者情報を取得できません。", "チャンネル所有者の Google アカウントで認可してください。"),
    "RUN_ALREADY_ACTIVE": ("同じ種類の収集がすでに実行中です。", "完了を待ってから再実行してください。"),
    "DATASET_NOT_READY": ("分析できるデータがまだありません。", "先にデータ収集を実行してください。"),
    "VIEW_NOT_FOUND_OR_FORBIDDEN": ("その保存条件は見つかりません。", "一覧から選び直してください。"),
    "INVALID_CURSOR": ("ページの位置が無効になりました。", "1 ページ目から表示し直してください。"),
    "CURSOR_EXPIRED": ("データが更新されたためページを表示できません。", "最新の結果を読み込み直してください。"),
    "INVALID_INPUT": ("入力内容を確認してください。", "値を修正して再度お試しください。"),
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


class AppError(Exception):
    def __init__(self, code: str, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def create_app(services: Services | None = None, *, base_url: str = "https://localhost") -> FastAPI:
    app = FastAPI(title="YouTube 分析", docs_url=None, redoc_url=None)
    app.state.services = services or build_services(base_url)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
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
        CSRF_COOKIE, csrf, httponly=False, secure=True, samesite="strict", path="/"
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

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        return _render(request, "login.html", {})

    @app.post("/login")
    async def login(
        request: Request, display_name: str = Form(...), csrf_token: str = Form("")
    ) -> Response:
        _check_csrf(request, csrf_token)
        subject = display_name.strip()
        if not subject or len(subject) > 80:
            raise AppError("INVALID_INPUT")
        issued = _services(request).access.establish_session(
            VerifiedIdentity(
                issuer="urn:demo:local",
                subject=subject,
                authenticated_at=_services(request).access._now(),
                display_name=subject,
            )
        )
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

    @app.post("/logout")
    async def logout(request: Request, csrf_token: str = Form("")) -> Response:
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
    async def dashboard(request: Request) -> Response:
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
                "runs": [
                    {
                        "kind": RUN_KIND_LABELS.get(item.kind.value, item.kind.value),
                        "status": RUN_STATUS_LABELS.get(
                            item.status.value, item.status.value
                        ),
                        "finished_at": item.finished_at,
                        "quota_spent": item.quota_spent,
                    }
                    for item in runs.items[:5]
                ],
            },
        )

    @app.post("/workspaces")
    async def create_workspace(
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
    async def select_workspace(
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
    async def start_connection(request: Request, csrf_token: str = Form("")) -> Response:
        _check_csrf(request, csrf_token)
        session = _require_session(request)
        context = _context(request, session, Permission.CHANNEL_MANAGE_CONNECTION)
        start = _services(request).connections.begin_authorization(
            context, BeginAuthorization(idempotency_key=secrets.token_urlsafe(16))
        )
        return RedirectResponse(start.authorization_url, status_code=303)

    @app.get("/demo/consent", response_class=HTMLResponse)
    async def demo_consent(request: Request, state: str = Query(...)) -> Response:
        return _render(request, "consent.html", {"state": state})

    @app.post("/oauth/callback")
    async def oauth_callback(
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
    async def disconnect(
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
    async def collect(
        request: Request, connection_id: str, csrf_token: str = Form("")
    ) -> Response:
        _check_csrf(request, csrf_token)
        session = _require_session(request)
        context = _context(request, session, Permission.COLLECTION_RUN)
        services = _services(request)
        outcome = "collected"
        for kind in (RunKind.SUBSCRIBERS, RunKind.OWNER_CONTENT):
            run = services.jobs.enqueue_run(
                context,
                EnqueueRun(
                    connection_id=connection_id,
                    kind=kind,
                    idempotency_key=secrets.token_urlsafe(16),
                ),
            )
            finished = services.jobs.execute_run(
                context,
                ExecuteRun(run_id=run.run_id, idempotency_key=secrets.token_urlsafe(16)),
            )
            if finished.status.value == "PARTIAL":
                outcome = "collect_partial"
            elif finished.status.value in {"FAILED", "QUEUED"}:
                outcome = "collect_failed"
                break
        return _redirect("/", outcome)

    @app.get("/analysis", response_class=HTMLResponse)
    async def analysis(
        request: Request,
        channel_id: str = Query(...),
        never_commented: bool = Query(False),
        subscribed_within_days: Annotated[OptionalInt, Query()] = None,
        segment: str | None = Query(None),
        cursor: str | None = Query(None),
    ) -> Response:
        session = _require_session(request)
        context = _context(request, session, Permission.ANALYSIS_READ)
        filters = AnalysisFilterInput(
            never_commented=never_commented,
            subscribed_within_days=subscribed_within_days,
            segments=(segment,) if segment else (),
        )
        page = _services(request).analysis.run_analysis(
            context,
            RunAnalysis(channel_id=channel_id, filters=filters),
            AnalysisPageRequest(cursor=cursor, limit=50),
        )
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
                "filters": {
                    "never_commented": never_commented,
                    "subscribed_within_days": subscribed_within_days,
                    "segment": segment,
                },
                "next_cursor": page.next_cursor,
                "segment_options": list(SEGMENT_LABELS.items()),
            },
        )

    @app.post("/analysis/export")
    async def export(
        request: Request,
        channel_id: str = Form(...),
        never_commented: bool = Form(False),
        csrf_token: str = Form(""),
    ) -> Response:
        _check_csrf(request, csrf_token)
        session = _require_session(request)
        context = _context(request, session, Permission.ANALYSIS_EXPORT)
        document = _services(request).analysis.export_analysis(
            context,
            ExportAnalysis(
                channel_id=channel_id,
                filters=AnalysisFilterInput(never_commented=never_commented),
            ),
        )
        return Response(
            content=document.content,
            media_type=document.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{document.filename}"'
            },
        )

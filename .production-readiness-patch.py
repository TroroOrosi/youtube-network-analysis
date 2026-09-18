"""Temporary, exact-context patch; removed after verified application."""
from pathlib import Path


def replace_once(path, old, new):
    file = Path(path)
    text = file.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise SystemExit('Source changed; inspect instead of overwriting: ' + path)
    file.write_text(text.replace(old, new, 1), encoding='utf-8')


replace_once('collection_jobs/service.py', '    # Reads\n', '''    # Reads

    def active_runs(
        self, context: WorkspaceContext, connection_id: str | None = None
    ) -> tuple[CollectionRun, ...]:
        """Unfinished work, independent of the paginated terminal history.

        Used to resume collection and determine completion. History display
        limits must never hide an old quota-suspended run. No provider calls.
        """
        _require(context, Permission.COLLECTION_READ)
        query = RunQuery(connection_id=connection_id)
        with self._lock:
            return tuple(sorted(
                (run for run in self._state.runs.values()
                 if run.workspace_id == context.workspace_id
                 and run.status in ACTIVE_STATUSES
                 and (query.connection_id is None
                      or run.connection_id == query.connection_id)),
                key=lambda run: (run.enqueued_at, run.run_id), reverse=True,
            ))
''')

replace_once('web_ui/app.py', 'from .gcp import GcpUnavailable\n',
             'from .gcp import GcpUnavailable\nfrom .runtime import install_operational_routes\n')
replace_once('web_ui/app.py', '    app.state.services = services or build_services(base_url)\n',
             '    app.state.services = services or build_services(base_url)\n'
             '    app.state.collection_start_lock = Lock()\n')
replace_once('web_ui/app.py', '    _register_routes(app)\n    return app\n',
             '    _register_routes(app)\n    install_operational_routes(app)\n    return app\n')
replace_once('web_ui/app.py',
             'def _error_response(request: Request, code: str, status_code: int) -> HTMLResponse:\n',
             'def _error_response(request: Request, code: str, status_code: int) -> HTMLResponse:\n'
             '    request.state.error_code = code\n')
replace_once('web_ui/app.py', '''    waiting = tuple(
        run for run in runs if run.status.value in {"QUEUED", "RUNNING"}
    )
''', '''    waiting = services.jobs.active_runs(context)
    active_ids = {run.run_id for run in waiting}
    runs = waiting + tuple(run for run in runs if run.run_id not in active_ids)
''')
replace_once('web_ui/app.py', '''        for kind in (RunKind.SUBSCRIBERS, RunKind.OWNER_CONTENT):
            services.jobs.enqueue_run(
                context,
                EnqueueRun(
                    connection_id=connection_id,
                    kind=kind,
                    idempotency_key=secrets.token_urlsafe(16),
                ),
            )
        return _redirect("/collecting")
''', '''        # Serialize browser starts, not collection execution. Repeated clicks
        # resume the existing batch instead of resetting a quota checkpoint or
        # re-collecting subscribers while content is still queued.
        with request.app.state.collection_start_lock:
            if services.jobs.active_runs(context, connection_id):
                return _redirect("/collecting")
            for kind in (RunKind.SUBSCRIBERS, RunKind.OWNER_CONTENT):
                try:
                    services.jobs.enqueue_run(
                        context,
                        EnqueueRun(
                            connection_id=connection_id,
                            kind=kind,
                            idempotency_key=secrets.token_urlsafe(16),
                        ),
                    )
                except CollectionJobsError as error:
                    # A scheduler may have queued work since the read above.
                    # Authorization/storage/input errors must still propagate.
                    if error.code != "RUN_ALREADY_ACTIVE":
                        raise
                    return _redirect("/collecting")
        return _redirect("/collecting")
''')
replace_once('web_ui/main.py', 'from .app import create_app\n',
             'from .runtime import validate_production_environment\n\n'
             '# Refuse incomplete hosted settings before constructing any service.\n'
             'validate_production_environment(os.environ)\n\n'
             'from .app import create_app\n')
replace_once('Dockerfile', 'ENV PORT=8080\n',
             'ENV PORT=8080 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1\n')
replace_once('Dockerfile', '--proxy-headers --no-access-log',
             '--workers 1 --proxy-headers --no-access-log')
print('Applied exact-context production changes.')

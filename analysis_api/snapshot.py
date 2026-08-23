"""Stable persistence format for saved analysis views.

Page cursors and idempotency records are intentionally process-local. Saved
views are user-authored workspace data and must survive a Cloud Run restart.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Iterable

from .models import AnalysisFilterInput, SavedView


VERSION = 1


def dump_views(views: Iterable[SavedView]) -> str:
    rows = [
        {
            "view_id": view.view_id,
            "workspace_id": view.workspace_id,
            "name": view.name,
            "channel_id": view.channel_id,
            "filters": {
                "subscribed_within_days": view.filters.subscribed_within_days,
                "subscribed_since": _date_text(view.filters.subscribed_since),
                "subscribed_until": _date_text(view.filters.subscribed_until),
                "never_commented": view.filters.never_commented,
                "no_comment_within_days": view.filters.no_comment_within_days,
                "include_not_seen_latest": view.filters.include_not_seen_latest,
                "segments": list(view.filters.segments),
            },
            "created_at": view.created_at.isoformat(),
            "updated_at": view.updated_at.isoformat(),
        }
        for view in sorted(views, key=lambda item: (item.workspace_id, item.view_id))
    ]
    return json.dumps(
        {"version": VERSION, "views": rows},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def load_views(document: str) -> tuple[SavedView, ...]:
    try:
        payload = json.loads(document)
        if not isinstance(payload, dict) or payload.get("version") != VERSION:
            raise ValueError("saved-view document version is unsupported")
        rows = payload.get("views")
        if not isinstance(rows, list):
            raise ValueError("saved-view document has no view list")
        return tuple(_view(row) for row in rows)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("saved-view document is not readable") from error


def _view(row: object) -> SavedView:
    if not isinstance(row, dict) or not isinstance(row.get("filters"), dict):
        raise ValueError("saved-view row is not an object")
    filters = row["filters"]
    assert isinstance(filters, dict)
    return SavedView(
        view_id=row["view_id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        channel_id=row["channel_id"],
        filters=AnalysisFilterInput(
            subscribed_within_days=filters.get("subscribed_within_days"),
            subscribed_since=_optional_date(filters.get("subscribed_since")),
            subscribed_until=_optional_date(filters.get("subscribed_until")),
            never_commented=filters.get("never_commented", False),
            no_comment_within_days=filters.get("no_comment_within_days"),
            include_not_seen_latest=filters.get("include_not_seen_latest", False),
            segments=tuple(filters.get("segments", ())),
        ),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("saved-view date is not text")
    return date.fromisoformat(value)


"""Validated, aggregate-only audience network report for the hosted UI."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, NamedTuple

PACKAGE_ROOT = Path(__file__).parent
AUDIENCE_REPORT_JSON = PACKAGE_ROOT / "data" / "audience-network-analysis.json"
AUDIENCE_REPORT_WORKBOOK = (
    PACKAGE_ROOT / "assets" / "audience-network-analysis.xlsx"
)


class ReportSummary(NamedTuple):
    viewers: int
    channels: int
    viewer_channel_edges: int
    network_nodes: int
    network_edges: int
    communities: int
    median_breadth: int


class ChannelShare(NamedTuple):
    channel: str
    viewers: int
    panel_share: float


class BreadthBucket(NamedTuple):
    label: str
    viewers: int
    share: float


class InterestCategory(NamedTuple):
    category: str
    channels: int
    viewer_links: int
    share: float


class Community(NamedTuple):
    rank: int
    channels: int
    edges: int
    internal_weight: float
    representatives: str


class NetworkRelationship(NamedTuple):
    left: str
    right: str
    shared_viewers: int
    weighted_cosine: float


class Affinity(NamedTuple):
    channel: str
    observed_viewers: int
    panel_share: float
    affinity_lift: float


class PopularityAffinity(NamedTuple):
    channel: str
    popularity_rank: int
    affinity_rank: int
    rank_gap: int
    panel_share: float
    affinity_lift: float


class AudienceReport(NamedTuple):
    title: str
    source_label: str
    coverage_period: str
    methodology: str
    summary: ReportSummary
    top_channels: tuple[ChannelShare, ...]
    viewer_breadth: tuple[BreadthBucket, ...]
    categories: tuple[InterestCategory, ...]
    communities: tuple[Community, ...]
    network_relationships: tuple[NetworkRelationship, ...]
    affinity: tuple[Affinity, ...]
    popularity_affinity: tuple[PopularityAffinity, ...]


TOP_LEVEL_FIELDS = frozenset(AudienceReport._fields) | {"schema_version"}


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _fields(value: dict[str, Any], expected: tuple[str, ...], name: str) -> None:
    unexpected = set(value) - set(expected)
    missing = set(expected) - set(value)
    if unexpected or missing:
        raise ValueError(f"unexpected report fields in {name}")


def _text(value: Any, name: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be bounded non-empty text")
    return value.strip()


def _integer(value: Any, name: str, *, allow_negative: bool = False) -> int:
    minimum = -10_000_000 if allow_negative else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be a valid integer")
    return value


def _number(value: Any, name: str, *, ratio: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (ratio and result > 1):
        raise ValueError(f"{name} must be a valid number")
    return result


def _rows(document: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = document[name]
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError(f"{name} must contain 1 to 100 rows")
    return [_object(row, f"{name} row") for row in value]


def load_audience_report(path: Path = AUDIENCE_REPORT_JSON) -> AudienceReport:
    """Load one allowlisted aggregate report and reject schema drift."""

    document = _object(json.loads(path.read_text(encoding="utf-8")), "report")
    if set(document) != TOP_LEVEL_FIELDS:
        raise ValueError("unexpected report fields at document root")
    if document["schema_version"] != 1:
        raise ValueError("unsupported audience report schema")

    summary_doc = _object(document["summary"], "summary")
    _fields(summary_doc, ReportSummary._fields, "summary")
    summary = ReportSummary(
        *(_integer(summary_doc[name], f"summary.{name}") for name in ReportSummary._fields)
    )

    top_channels = tuple(
        _channel_share(row, "top_channels") for row in _rows(document, "top_channels")
    )
    viewer_breadth = tuple(
        _breadth(row) for row in _rows(document, "viewer_breadth")
    )
    categories = tuple(_category(row) for row in _rows(document, "categories"))
    communities = tuple(_community(row) for row in _rows(document, "communities"))
    relationships = tuple(
        _relationship(row) for row in _rows(document, "network_relationships")
    )
    affinity = tuple(_affinity(row) for row in _rows(document, "affinity"))
    popularity = tuple(
        _popularity_affinity(row) for row in _rows(document, "popularity_affinity")
    )
    return AudienceReport(
        _text(document["title"], "title"),
        _text(document["source_label"], "source_label"),
        _text(document["coverage_period"], "coverage_period"),
        _text(document["methodology"], "methodology", maximum=1_000),
        summary,
        top_channels,
        viewer_breadth,
        categories,
        communities,
        relationships,
        affinity,
        popularity,
    )


def _channel_share(row: dict[str, Any], name: str) -> ChannelShare:
    _fields(row, ChannelShare._fields, name)
    return ChannelShare(
        _text(row["channel"], f"{name}.channel"),
        _integer(row["viewers"], f"{name}.viewers"),
        _number(row["panel_share"], f"{name}.panel_share", ratio=True),
    )


def _breadth(row: dict[str, Any]) -> BreadthBucket:
    _fields(row, BreadthBucket._fields, "viewer_breadth")
    return BreadthBucket(
        _text(row["label"], "viewer_breadth.label"),
        _integer(row["viewers"], "viewer_breadth.viewers"),
        _number(row["share"], "viewer_breadth.share", ratio=True),
    )


def _category(row: dict[str, Any]) -> InterestCategory:
    _fields(row, InterestCategory._fields, "categories")
    return InterestCategory(
        _text(row["category"], "categories.category"),
        _integer(row["channels"], "categories.channels"),
        _integer(row["viewer_links"], "categories.viewer_links"),
        _number(row["share"], "categories.share", ratio=True),
    )


def _community(row: dict[str, Any]) -> Community:
    _fields(row, Community._fields, "communities")
    return Community(
        _integer(row["rank"], "communities.rank"),
        _integer(row["channels"], "communities.channels"),
        _integer(row["edges"], "communities.edges"),
        _number(row["internal_weight"], "communities.internal_weight"),
        _text(row["representatives"], "communities.representatives", maximum=2_000),
    )


def _relationship(row: dict[str, Any]) -> NetworkRelationship:
    _fields(row, NetworkRelationship._fields, "network_relationships")
    return NetworkRelationship(
        _text(row["left"], "network_relationships.left"),
        _text(row["right"], "network_relationships.right"),
        _integer(row["shared_viewers"], "network_relationships.shared_viewers"),
        _number(
            row["weighted_cosine"], "network_relationships.weighted_cosine", ratio=True
        ),
    )


def _affinity(row: dict[str, Any]) -> Affinity:
    _fields(row, Affinity._fields, "affinity")
    return Affinity(
        _text(row["channel"], "affinity.channel"),
        _integer(row["observed_viewers"], "affinity.observed_viewers"),
        _number(row["panel_share"], "affinity.panel_share", ratio=True),
        _number(row["affinity_lift"], "affinity.affinity_lift"),
    )


def _popularity_affinity(row: dict[str, Any]) -> PopularityAffinity:
    _fields(row, PopularityAffinity._fields, "popularity_affinity")
    return PopularityAffinity(
        _text(row["channel"], "popularity_affinity.channel"),
        _integer(row["popularity_rank"], "popularity_affinity.popularity_rank"),
        _integer(row["affinity_rank"], "popularity_affinity.affinity_rank"),
        _integer(row["rank_gap"], "popularity_affinity.rank_gap", allow_negative=True),
        _number(row["panel_share"], "popularity_affinity.panel_share", ratio=True),
        _number(row["affinity_lift"], "popularity_affinity.affinity_lift"),
    )

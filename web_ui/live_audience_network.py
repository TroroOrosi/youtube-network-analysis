"""Build aggregate-only audience-network reports from live collected snapshots."""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
from math import sqrt
from statistics import median

from collection_jobs.models import AudienceNetworkSnapshot

from .audience_report import (
    Affinity,
    AudienceReport,
    BreadthBucket,
    ChannelShare,
    Community,
    InterestCategory,
    NetworkRelationship,
    PopularityAffinity,
    ReportSummary,
)


_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ビジネス・投資・お金", ("ビジネス", "投資", "株", "お金", "経済", "business", "money")),
    ("自己啓発・学び", ("自己啓発", "学び", "大学", "勉強", "study", "心理", "習慣")),
    ("読書・要約・教養", ("本", "読書", "要約", "教養", "歴史", "book")),
    ("科学・数学・テクノロジー", ("科学", "数学", "ai", "テック", "tech", "science")),
    ("エンタメ・音楽・芸能", ("音楽", "music", "映画", "アニメ", "ゲーム", "game")),
    ("ニュース・社会", ("ニュース", "news", "政治", "社会", "時事")),
    ("美容・健康・生活", ("美容", "健康", "料理", "生活", "health", "cook")),
)


def _category(title: str) -> str:
    lowered = title.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword.lower() in lowered for keyword in keywords):
            return category
    return "その他・未分類"


def _breadth_buckets(values: list[int]) -> tuple[BreadthBucket, ...]:
    if not values:
        return ()
    boundaries = (
        (0, 0, "0"),
        (1, 9, "1〜9"),
        (10, 49, "10〜49"),
        (50, 99, "50〜99"),
        (100, 249, "100〜249"),
        (250, 499, "250〜499"),
        (500, 749, "500〜749"),
        (750, 10_000_000, "750以上"),
    )
    total = len(values)
    rows = []
    for low, high, label in boundaries:
        count = sum(1 for value in values if low <= value <= high)
        if count:
            rows.append(BreadthBucket(label, count, count / total))
    return tuple(rows)


def _relationships(
    viewer_sets: list[set[str]],
    counts: Counter[str],
    titles: dict[str, str],
) -> tuple[NetworkRelationship, ...]:
    """Bound pair growth by projecting only the 100 most-observed channels."""

    top_ids = {
        channel_id
        for channel_id, _ in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )[:100]
    }
    shared: Counter[tuple[str, str]] = Counter()
    for channels in viewer_sets:
        selected = sorted(channels.intersection(top_ids))
        shared.update(combinations(selected, 2))

    rows = []
    for (left, right), viewers in shared.items():
        if viewers < 3:
            continue
        cosine = viewers / sqrt(counts[left] * counts[right])
        if cosine < 0.12:
            continue
        rows.append(
            NetworkRelationship(
                titles.get(left, left),
                titles.get(right, right),
                viewers,
                min(1.0, cosine),
            )
        )
    rows.sort(key=lambda row: (-row.shared_viewers, -row.weighted_cosine, row.left, row.right))
    return tuple(rows[:100])


def _communities(
    relationships: tuple[NetworkRelationship, ...],
) -> tuple[Community, ...]:
    """Report connected groups of strong live relationships without claiming Louvain."""

    adjacency: dict[str, set[str]] = defaultdict(set)
    weights: dict[tuple[str, str], float] = {}
    for row in relationships:
        adjacency[row.left].add(row.right)
        adjacency[row.right].add(row.left)
        weights[tuple(sorted((row.left, row.right)))] = row.weighted_cosine

    seen: set[str] = set()
    groups: list[tuple[set[str], int, float]] = []
    for node in sorted(adjacency):
        if node in seen:
            continue
        stack = [node]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component.add(current)
            stack.extend(adjacency[current] - seen)
        edges = {
            tuple(sorted((left, right)))
            for left in component
            for right in adjacency[left]
            if right in component and left != right
        }
        groups.append(
            (
                component,
                len(edges),
                sum(weights.get(edge, 0.0) for edge in edges),
            )
        )

    groups.sort(key=lambda item: (-len(item[0]), -item[1], sorted(item[0])))
    return tuple(
        Community(
            rank=index,
            channels=len(nodes),
            edges=edge_count,
            internal_weight=weight,
            representatives=" / ".join(sorted(nodes)[:5]),
        )
        for index, (nodes, edge_count, weight) in enumerate(groups[:50], start=1)
    )


def build_live_audience_report(snapshot: AudienceNetworkSnapshot) -> AudienceReport:
    public_viewers = [viewer for viewer in snapshot.viewers if viewer.public]
    viewer_sets: list[set[str]] = []
    titles: dict[str, str] = {}
    counts: Counter[str] = Counter()

    for viewer in public_viewers:
        channels: set[str] = set()
        for row in viewer.subscriptions:
            channels.add(row.channel_id)
            titles.setdefault(row.channel_id, row.title)
        viewer_sets.append(channels)
        counts.update(channels)

    viewer_count = len(public_viewers)
    top = sorted(counts.items(), key=lambda item: (-item[1], titles.get(item[0], item[0])))
    top_channels = tuple(
        ChannelShare(
            titles.get(channel_id, channel_id),
            count,
            count / viewer_count if viewer_count else 0.0,
        )
        for channel_id, count in top[:50]
    )

    category_channels: dict[str, set[str]] = defaultdict(set)
    category_links: Counter[str] = Counter()
    for channel_id, count in counts.items():
        category = _category(titles.get(channel_id, channel_id))
        category_channels[category].add(channel_id)
        category_links[category] += count
    total_links = sum(category_links.values())
    categories = tuple(
        InterestCategory(
            category,
            len(category_channels[category]),
            links,
            links / total_links if total_links else 0.0,
        )
        for category, links in sorted(
            category_links.items(), key=lambda item: (-item[1], item[0])
        )
    )

    relationships = _relationships(viewer_sets, counts, titles)
    communities = _communities(relationships)
    breadth = [len(channels) for channels in viewer_sets]
    edges = sum(breadth)

    public_count = len(public_viewers)
    unavailable_count = len(snapshot.viewers) - public_count
    methodology = (
        "最新の動画コメント投稿者を視聴者の代理とし、各投稿者が公開している"
        "チャンネル登録先を YouTube Data API から収集したライブ集計です。"
        f"公開登録を取得できた投稿者 {public_count} 人、非公開・取得不可 {unavailable_count} 人。"
        "非公開の登録関係は推測しません。強い関係は観測上位100チャンネル内で"
        "共通視聴者3人以上かつコサイン類似度0.12以上に限定しています。"
        "外部の総登録者数を追加取得していないため、人気度補正liftはライブ版では表示しません。"
    )

    return AudienceReport(
        title="視聴者ネットワーク分析",
        source_label="接続チャンネルの最新ライブ収集",
        coverage_period=f"収集完了: {snapshot.captured_at.astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        methodology=methodology,
        summary=ReportSummary(
            viewers=viewer_count,
            channels=len(counts),
            viewer_channel_edges=edges,
            network_nodes=len({name for row in relationships for name in (row.left, row.right)}),
            network_edges=len(relationships),
            communities=len(communities),
            median_breadth=int(median(breadth)) if breadth else 0,
        ),
        top_channels=top_channels,
        viewer_breadth=_breadth_buckets(breadth),
        categories=categories,
        communities=communities,
        network_relationships=relationships,
        affinity=tuple(),
        popularity_affinity=tuple(),
    )

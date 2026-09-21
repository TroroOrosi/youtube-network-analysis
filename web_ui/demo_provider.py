"""Synthetic provider gateways for the reference web application.

These gateways never contact Google or any network. They exist so the whole
hosted flow can be operated and reviewed before a real provider adapter, client
registration, and consent screen are approved. A production deployment must
replace them with audited HTTP/SDK adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from channel_connections.models import (
    APPROVED_SCOPES,
    ChannelSubscriptionRow,
    CommentAuthorRow,
    ConnectionProvider,
    ProviderCredential,
    ProviderPage,
    RedactedSecret,
    RevocationOutcome,
    SubscriberRow,
    VerifiedProviderGrant,
    VideoRow,
)


DEMO_CHANNEL_ID = "UC_demo_channel"
DEMO_CHANNEL_TITLE = "デモチャンネル"
QUOTA_COST = 3


@dataclass(frozen=True, slots=True)
class DemoDataSet:
    subscribers: tuple[SubscriberRow, ...]
    videos: tuple[VideoRow, ...]
    comment_authors: dict[str, tuple[CommentAuthorRow, ...]]
    channel_subscriptions: dict[str, tuple[ChannelSubscriptionRow, ...] | None]


def build_demo_dataset(now: datetime | None = None) -> DemoDataSet:
    reference = now or datetime.now(UTC)
    ages = (2, 20, 45, 150, 320, 400)
    subscribers = tuple(
        SubscriberRow(
            subscriber_channel_id=f"UC_demo_sub_{index}",
            title=f"視聴者{index}",
            api_published_at=reference - timedelta(days=age),
        )
        for index, age in enumerate(ages, start=1)
    )
    videos = tuple(
        VideoRow(
            video_id=f"demo-video-{index}",
            title=f"デモ動画{index}",
            published_at=reference - timedelta(days=index * 30),
        )
        for index in range(1, 4)
    )
    comment_authors = {
        "demo-video-1": (
            CommentAuthorRow(
                "demo-video-1", "UC_demo_sub_1", 4, reference - timedelta(days=3)
            ),
            CommentAuthorRow(
                "demo-video-1", "UC_demo_sub_2", 1, reference - timedelta(days=200)
            ),
        ),
        "demo-video-2": (
            CommentAuthorRow(
                "demo-video-2", "UC_demo_sub_1", 2, reference - timedelta(days=10)
            ),
        ),
        "demo-video-3": (),
    }
    channel_subscriptions = {
        "UC_demo_sub_1": (
            ChannelSubscriptionRow("UC_demo_other_1", "学びチャンネル"),
            ChannelSubscriptionRow("UC_demo_shared", "共通チャンネル"),
        ),
        "UC_demo_sub_2": (
            ChannelSubscriptionRow("UC_demo_other_2", "ニュースチャンネル"),
            ChannelSubscriptionRow("UC_demo_shared", "共通チャンネル"),
        ),
    }
    return DemoDataSet(
        subscribers=subscribers,
        videos=videos,
        comment_authors=comment_authors,
        channel_subscriptions=channel_subscriptions,
    )


class DemoAuthorizationGateway:
    """Renders an in-app consent URL instead of calling Google."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self.revocations: list[tuple[str, str]] = []

    def authorization_url(
        self,
        *,
        state: RedactedSecret,
        code_challenge: str,
        redirect_uri_id: str,
        scopes: tuple[str, ...],
    ) -> str:
        return (
            f"{self._base_url}/demo/consent"
            f"?state={state.reveal()}"
            f"&code_challenge={code_challenge}"
            f"&redirect_uri_id={redirect_uri_id}"
            f"&scope={scopes[0]}"
        )

    def exchange_and_verify(
        self,
        *,
        code: RedactedSecret,
        code_verifier: RedactedSecret,
        redirect_uri_id: str,
    ) -> VerifiedProviderGrant:
        return VerifiedProviderGrant(
            provider=ConnectionProvider.YOUTUBE,
            provider_channel_id=DEMO_CHANNEL_ID,
            channel_title=DEMO_CHANNEL_TITLE,
            granted_scopes=APPROVED_SCOPES,
            credential=ProviderCredential(
                access_token=RedactedSecret("demo-access-token"),
                refresh_token=RedactedSecret("demo-refresh-token"),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=APPROVED_SCOPES,
            ),
            subscriber_capability_verified=True,
        )

    def revoke(self, workspace_id: str, credential_slot_id: str) -> RevocationOutcome:
        self.revocations.append((workspace_id, credential_slot_id))
        return RevocationOutcome.REVOKED


class DemoDataGateway:
    """Serves the synthetic dataset with provider-like paging and quota costs."""

    def __init__(self, dataset: DemoDataSet | None = None) -> None:
        self._dataset = dataset or build_demo_dataset()

    def list_subscribers(
        self,
        workspace_id: str,
        credential_slot_id: str,
        *,
        page_token: str | None,
        max_results: int,
    ) -> ProviderPage:
        return _page(self._dataset.subscribers, page_token, max_results)

    def list_channel_subscriptions(
        self,
        workspace_id: str,
        credential_slot_id: str,
        *,
        channel_id: str,
        page_token: str | None,
        max_results: int,
    ) -> ProviderPage:
        rows = self._dataset.channel_subscriptions.get(channel_id)
        if rows is None:
            return ProviderPage(
                rows=(), next_page_token=None,
                quota_cost=QUOTA_COST, accessible=False,
            )
        return _page(rows, page_token, max_results)

    def list_videos(
        self,
        workspace_id: str,
        credential_slot_id: str,
        *,
        page_token: str | None,
        max_results: int,
    ) -> ProviderPage:
        return _page(self._dataset.videos, page_token, max_results)

    def list_video_comment_authors(
        self,
        workspace_id: str,
        credential_slot_id: str,
        *,
        video_id: str,
        page_token: str | None,
        max_results: int,
    ) -> ProviderPage:
        rows = self._dataset.comment_authors.get(video_id, ())
        return _page(rows, page_token, max_results)


def _page(
    rows: tuple[object, ...], page_token: str | None, max_results: int
) -> ProviderPage:
    offset = int(page_token.removeprefix("page-")) if page_token else 0
    window = rows[offset : offset + max_results]
    next_offset = offset + max_results
    return ProviderPage(
        rows=tuple(window),
        next_page_token=f"page-{next_offset}" if next_offset < len(rows) else None,
        quota_cost=QUOTA_COST,
    )

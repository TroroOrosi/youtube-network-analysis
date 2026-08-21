"""Stable synchronous ports for tenant-scoped channel data."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from workspace_access.models import WorkspaceContext

from .models import (
    ChannelDataFreshness,
    ChannelDataRetentionReport,
    CollectionHistoryQuery,
    CollectionState,
    CommentCoverage,
    DeleteChannelData,
    DeleteWorkspaceData,
    FinishCollection,
    Page,
    PageRequest,
    PublishSubscriberSnapshot,
    PublishVideoInventory,
    ReplaceVideoCommentActivity,
    SilentAnalysisDataset,
    StartCollection,
    SubscriberRegistryEntry,
    SubscriberRegistryQuery,
    SubscriberSnapshot,
    SubscriberSnapshotQuery,
    VideoInventory,
)


class CollectionWriter(Protocol):
    def start_collection(
        self, context: WorkspaceContext, command: StartCollection
    ) -> CollectionState: ...

    def publish_subscriber_snapshot(
        self, context: WorkspaceContext, command: PublishSubscriberSnapshot
    ) -> SubscriberSnapshot: ...

    def publish_video_inventory(
        self, context: WorkspaceContext, command: PublishVideoInventory
    ) -> VideoInventory: ...

    def replace_video_comment_activity(
        self, context: WorkspaceContext, command: ReplaceVideoCommentActivity
    ) -> CommentCoverage: ...

    def finish_collection(
        self, context: WorkspaceContext, command: FinishCollection
    ) -> CollectionState: ...


class CollectionReader(Protocol):
    def get_freshness(
        self, context: WorkspaceContext, channel_id: str
    ) -> ChannelDataFreshness: ...

    def list_collection_history(
        self,
        context: WorkspaceContext,
        query: CollectionHistoryQuery,
        page: PageRequest = PageRequest(),
    ) -> Page[CollectionState]: ...


class AnalysisDataReader(Protocol):
    def load_silent_analysis_dataset(
        self, context: WorkspaceContext, channel_id: str
    ) -> SilentAnalysisDataset: ...

    def list_subscriber_registry(
        self,
        context: WorkspaceContext,
        query: SubscriberRegistryQuery,
        page: PageRequest = PageRequest(),
    ) -> Page[SubscriberRegistryEntry]: ...

    def list_subscriber_snapshots(
        self,
        context: WorkspaceContext,
        query: SubscriberSnapshotQuery,
        page: PageRequest = PageRequest(),
    ) -> Page[SubscriberSnapshot]: ...


class ChannelDataAdministrator(Protocol):
    def delete_channel_data(
        self, context: WorkspaceContext, command: DeleteChannelData
    ) -> None: ...

    def delete_workspace_data(
        self, context: WorkspaceContext, command: DeleteWorkspaceData
    ) -> None: ...

    def purge_retention(
        self, context: WorkspaceContext, reference_time: datetime
    ) -> ChannelDataRetentionReport: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class TokenGenerator(Protocol):
    def new_token(self) -> str: ...


class StateStore(Protocol):
    """Where this module's state document rests between two processes.

    The store never learns what is inside: it keeps one text and hands it back
    unchanged. A file, a row, or nothing at all is a deployment decision.
    """

    def load(self) -> str | None: ...

    def save(self, document: str) -> None: ...

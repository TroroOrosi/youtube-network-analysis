"""Tenant-scoped channel-connection contracts.

Import immutable values from :mod:`channel_connections.models`; the package root
stays small so credential, provider, and transport details cannot become
accidental API surface.
"""

from .errors import ChannelConnectionsError, ErrorCode
from .ports import (
    CollectionTargetResolver,
    ConnectionExecutionBroker,
    ConnectionManager,
    ConnectionPrivacyAdministrator,
    ConnectionReader,
)

__all__ = (
    "ChannelConnectionsError",
    "CollectionTargetResolver",
    "ConnectionExecutionBroker",
    "ConnectionManager",
    "ConnectionPrivacyAdministrator",
    "ConnectionReader",
    "ErrorCode",
)

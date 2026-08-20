"""Tenant-scoped channel-data contracts.

Import immutable values from :mod:`channel_data.models`; the package root stays
small so persistence and provider details cannot become accidental API surface.
"""

from .errors import ChannelDataError, ErrorCode
from .ports import (
    AnalysisDataReader,
    ChannelDataAdministrator,
    CollectionReader,
    CollectionWriter,
)

__all__ = (
    "AnalysisDataReader",
    "ChannelDataAdministrator",
    "ChannelDataError",
    "CollectionReader",
    "CollectionWriter",
    "ErrorCode",
)

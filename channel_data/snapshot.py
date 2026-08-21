"""The state document this module writes and reads back.

The collected data is the expensive part of this product: every row cost
provider quota, and losing it on a restart means asking YouTube for it again.
The format belongs here, not to the store, which only keeps one text.

Every value is tagged with what it is, so reading a document needs no type
hints and no guessing: a record written by this module comes back as the same
dataclass or not at all. `VERSION` refuses a document written by code that knew
more than this one does, because a silently dropped field is a silently wrong
dataset.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from . import memory, models
from .memory import MemoryState

VERSION = 1


def _known_types() -> dict[str, type]:
    found: dict[str, type] = {}
    for module in (models, memory):
        for value in vars(module).values():
            if isinstance(value, type) and (
                is_dataclass(value) or issubclass(value, Enum)
            ):
                found[value.__name__] = value
    return found


_TYPES = _known_types()


def dump(state: MemoryState) -> str:
    return json.dumps(
        {"version": VERSION, "state": _encode(state)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def load(document: str) -> MemoryState:
    try:
        payload = json.loads(document)
    except ValueError as error:
        raise ValueError("state document is not readable JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("state document is not an object")
    if payload.get("version") != VERSION:
        raise ValueError(
            f"state document version {payload.get('version')!r} is not supported"
        )
    state = _decode(payload.get("state"))
    if not isinstance(state, MemoryState):
        raise ValueError("state document does not hold this module's state")
    return state


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        # Before the primitive check: these enums are also `str`, and writing
        # one as a bare string would read back as a string the models refuse.
        return {"#": "enum", "t": type(value).__name__, "v": value.value}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return {"#": "time", "v": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "#": "obj",
            "t": type(value).__name__,
            "f": {
                field.name: _encode(getattr(value, field.name))
                for field in fields(value)
            },
        }
    if isinstance(value, tuple):
        return {"#": "tuple", "v": [_encode(item) for item in value]}
    if isinstance(value, list):
        return {"#": "list", "v": [_encode(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        return {"#": "set", "v": [_encode(item) for item in value]}
    if isinstance(value, dict):
        return {
            "#": "map",
            "v": [[_encode(key), _encode(item)] for key, item in value.items()],
        }
    raise TypeError(f"{type(value).__name__} cannot be written to a state document")


def _decode(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if not isinstance(value, dict):
        raise ValueError("state document holds an unexpected value")
    kind = value.get("#")
    if kind == "time":
        return datetime.fromisoformat(value["v"])
    if kind == "enum":
        return _type(value["t"])(value["v"])
    if kind == "obj":
        target = _type(value["t"])
        return target(
            **{name: _decode(item) for name, item in value["f"].items()}
        )
    if kind == "tuple":
        return tuple(_decode(item) for item in value["v"])
    if kind == "list":
        return [_decode(item) for item in value["v"]]
    if kind == "set":
        return frozenset(_decode(item) for item in value["v"])
    if kind == "map":
        return {_decode(key): _decode(item) for key, item in value["v"]}
    raise ValueError(f"state document holds an unknown tag {kind!r}")


def _type(name: str) -> type:
    target = _TYPES.get(name)
    if target is None:
        raise ValueError(f"state document names an unknown type {name!r}")
    return target

# SPDX-License-Identifier: GPL-3.0-or-later
"""The action request Gloxi sends to the gate (permissions.md, section 3).

Only what Gloxi *proposes* is read from the request. Whatever decides the
outcome (risk level, taint, the action id in the journal) is set by Herkos;
if the model sends such fields, they are ignored.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

MAX_RESOURCES = 1000
MAX_REASON = 500
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
# Fields the model may send but Herkos sets itself (permissions.md, section 3).
IGNORED_FIELDS = {"action_id", "level", "tainted", "risk"}
_ALLOWED_FIELDS = {"tool", "operation", "resources", "reason", "origin", "arguments"}


class RequestError(ValueError):
    """The request is malformed; it is denied without being assessed."""


@dataclass(frozen=True)
class Resource:
    uri: str
    scheme: str
    # For file:// resources: the absolute, normalised path (not yet resolved
    # through symlinks; the policy does that against the real filesystem).
    path: str | None = None


@dataclass(frozen=True)
class ActionRequest:
    tool: str
    operation: str
    resources: tuple[Resource, ...]
    reason: str
    user_turn: int | None = None
    # Tool arguments are passed through to the tool, never interpreted here.
    arguments: Mapping[str, Any] = field(default_factory=dict)


def parse_resource(uri: Any) -> Resource:
    if not isinstance(uri, str) or not uri or len(uri) > 4096:
        raise RequestError("each resource must be a non-empty URI string")
    if "\x00" in uri:
        raise RequestError("resource URI contains a NUL byte")
    parts = urlsplit(uri)
    scheme = parts.scheme.lower()
    if not scheme:
        raise RequestError(f"resource is not a URI: {uri!r}")
    if scheme != "file":
        return Resource(uri=uri, scheme=scheme)
    if parts.netloc not in ("", "localhost"):
        raise RequestError(f"file URI must be local: {uri!r}")
    path = unquote(parts.path)
    if "\x00" in path or not path.startswith("/"):
        raise RequestError(f"file URI must hold an absolute path: {uri!r}")
    # normpath folds "a/../b" lexically; symlinks are resolved by the policy.
    return Resource(uri=uri, scheme=scheme, path=posixpath.normpath(path))


def parse_request(params: Any) -> ActionRequest:
    if not isinstance(params, Mapping):
        raise RequestError("params must be an object")
    unknown = set(params) - _ALLOWED_FIELDS - IGNORED_FIELDS
    if unknown:
        raise RequestError(f"unknown fields: {sorted(unknown)}")

    tool, operation = params.get("tool"), params.get("operation")
    if not isinstance(tool, str) or not _NAME.match(tool):
        raise RequestError("'tool' must be a tool name such as org.vilna.mcp.files")
    if not isinstance(operation, str) or not _NAME.match(operation):
        raise RequestError("'operation' must be an operation name")

    raw_resources = params.get("resources", [])
    if not isinstance(raw_resources, list):
        raise RequestError("'resources' must be a list of URIs")
    if len(raw_resources) > MAX_RESOURCES:
        raise RequestError(f"at most {MAX_RESOURCES} resources per request")
    resources = tuple(parse_resource(r) for r in raw_resources)

    reason = params.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise RequestError("'reason' (an explanation for the user) is required")
    if len(reason) > MAX_REASON:
        raise RequestError(f"'reason' is longer than {MAX_REASON} characters")

    user_turn = None
    origin = params.get("origin", {})
    if not isinstance(origin, Mapping):
        raise RequestError("'origin' must be an object")
    if "user_turn" in origin:
        user_turn = origin["user_turn"]
        if not isinstance(user_turn, int) or isinstance(user_turn, bool) or user_turn < 0:
            raise RequestError("'origin.user_turn' must be a non-negative integer")

    arguments = params.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise RequestError("'arguments' must be an object")

    return ActionRequest(
        tool=tool,
        operation=operation,
        resources=resources,
        reason=reason.strip(),
        user_turn=user_turn,
        arguments=dict(arguments),
    )

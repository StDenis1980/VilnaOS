# SPDX-License-Identifier: GPL-3.0-or-later
"""Model router: decide whether a message goes to the local model or the cloud.

Rules, in order (roadmap 2.3):

1. The cloud is used only if the user allowed it and configured their own
   provider. ``cloud_allowed = false`` means nothing ever leaves the device.
2. An explicit choice for one message (``/local`` or ``/cloud``) wins.
3. ``mode = "local"`` or ``"cloud"`` pins every message.
4. ``mode = "auto"``: simple messages stay local; long ones go to the cloud.

Passwords and keys the user types are hidden from every model, the local one
too, and are never put back into an answer: models repeat them (ADR 0008).

The router does not fall back to the cloud on its own when the local model
fails: sending data somewhere the user did not choose is worse than an error.
These heuristics are a starting point and will be tuned on the November
measurements (roadmap 2.4).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from gettext import gettext as _
from typing import Literal, Protocol
from urllib.parse import urlsplit

from gloxi.config import ROUTER_MODES, RouterPolicy
from gloxi.i18n import N_
from gloxi.llm import ChatClient, Message, ToolCall

Target = Literal["local", "cloud"]


class CloudGuard(Protocol):
    """Masks what goes to the cloud and puts the real values back in the answer.

    ``privacy_gateway.Masker`` is one. A guard lives as long as one
    conversation, so the same value keeps the same placeholder across turns.
    """

    def mask_messages(self, messages: Iterable[Message]) -> list[Message]: ...

    def unmask_stream(self, chunks: Iterable[str]) -> Iterator[str]: ...


GuardFactory = Callable[[], CloudGuard]


class CloudNotAllowed(Exception):
    """The user asked for the cloud, but it is off or not configured."""


class NetworkRefused(CloudNotAllowed):
    """The cloud is on, but Gloxi may not reach the internet (ADR 0025, section 3).

    ``str(exc)`` is already in the person's language.
    """


# Opens the way to the internet before the first message to the cloud, or
# raises NetworkRefused. The system service asks Herkos (gloxi.network).
NetworkGate = Callable[[], None]


@dataclass(frozen=True)
class Route:
    target: Target
    reason: str
    # Where the data goes, for showing to the user (Risk-9 in the roadmap).
    destination: str


class Router:
    def __init__(
        self,
        local: ChatClient,
        cloud: ChatClient | None,
        policy: RouterPolicy,
        *,
        guard_factory: GuardFactory | None = None,
        network: NetworkGate | None = None,
    ) -> None:
        self.local = local
        self.network = network
        self.cloud = cloud
        self.policy = policy
        self._guard_factory = guard_factory
        self.guard: CloudGuard | None = guard_factory() if guard_factory else None
        self.secrets = _secret_hider()

    @property
    def cloud_enabled(self) -> bool:
        return self.policy.cloud_allowed and self.cloud is not None

    @property
    def masks_cloud(self) -> bool:
        return self.guard is not None

    @property
    def mode(self) -> str:
        return self.policy.mode

    def set_mode(self, mode: str) -> None:
        """Pick the model by hand, also in the middle of a conversation.

        It holds from the next message until changed again or Gloxi closes;
        the config file is not touched.
        """
        if mode not in ROUTER_MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if mode == "cloud" and not self.cloud_enabled:
            raise CloudNotAllowed("the cloud is turned off")
        self.policy = replace(self.policy, mode=mode)

    def new_conversation(self) -> None:
        """Forget the masked values of the previous conversation."""
        self.secrets = _secret_hider()
        if self._guard_factory:
            self.guard = self._guard_factory()

    def decide(self, text: str, *, force: Target | None = None) -> Route:
        if force == "cloud" or (force is None and self.policy.mode == "cloud"):
            if not self.cloud_enabled:
                raise CloudNotAllowed(
                    "the cloud is turned off; set cloud_allowed = true and configure [cloud]"
                )
            why = "asked for this message" if force else "mode = cloud"
            return self._route("cloud", why)
        if force == "local":
            return self._route("local", "asked for this message")
        if self.policy.mode == "local":
            return self._route("local", "mode = local")
        if not self.cloud_enabled:
            return self._route("local", "cloud is off")
        if len(text) > self.policy.long_message_chars:
            return self._route("cloud", f"longer than {self.policy.long_message_chars} characters")
        return self._route("local", "simple message")

    def stream(self, messages: Sequence[Message], route: Route) -> Iterator[str]:
        # Passwords are only shown as dots in the answer, so the model needs no
        # hint about their placeholders (and the local model no extra prompt).
        messages = self.secrets.mask_messages(messages, hint=False)
        yield from self.secrets.conceal_stream(self._stream(messages, route))

    def _stream(self, messages: Sequence[Message], route: Route) -> Iterator[str]:
        if route.target == "local":
            yield from self.local.stream(messages)
            return
        if not self.cloud_enabled:
            raise CloudNotAllowed("the cloud is turned off")
        if self.network is not None:
            self.network()
        if self.guard is None:
            yield from self.cloud.stream(messages)
            return
        masked = self.guard.mask_messages(messages)
        answer = []
        for piece in self.guard.unmask_stream(self.cloud.stream(masked)):
            answer.append(piece)
            yield piece
        # The model may have changed a placeholder beyond recognition, so a
        # real value is missing from the answer: say so rather than hide it.
        unresolved = getattr(self.guard, "unresolved", None)
        if unresolved and unresolved("".join(answer)):
            yield "\n\n" + _(MISSING_VALUES_NOTE)

    def stream_events(
        self, messages: Sequence[Message], route: Route, tools: Sequence[dict] | None = None
    ) -> Iterator[str | ToolCall]:
        """``stream`` with tools. Tools are offered to the local model only.

        File contents and paths from tools stay on this computer: the cloud
        model gets no tools, and the conversation keeps no tool messages.
        """
        if route.target != "local":
            yield from self.stream(messages, route)
            return
        held = ""
        for event in self.local.stream_events(
            self.secrets.mask_messages(messages, hint=False), tools
        ):
            if isinstance(event, str):
                held += event
                # Hold back a tail that may still become a placeholder: "[SEC".
                tail = _PARTIAL_PLACEHOLDER.search(held)
                cut = tail.start() if tail else len(held)
                if cut:
                    yield self.secrets.conceal(held[:cut])
                    held = held[cut:]
                continue
            if held:
                yield self.secrets.conceal(held)
                held = ""
            yield event
        if held:
            yield self.secrets.conceal(held)

    def _route(self, target: Target, reason: str) -> Route:
        client = self.local if target == "local" else self.cloud
        destination = "this computer" if target == "local" else _host(client.endpoint.url)
        return Route(target=target, reason=reason, destination=destination)


MISSING_VALUES_NOTE = N_(
    "Note: the cloud model changed some masked values, so they could not be "
    "put back. Check the answer before you use it."
)

_PARTIAL_PLACEHOLDER = re.compile(r"\[[A-Z_]{0,12}\d{0,4}$")


def _secret_hider():
    from privacy_gateway import Kind, Masker

    return Masker(kinds=[Kind.SECRET])


def _host(url: str) -> str:
    return urlsplit(url).hostname or url

# SPDX-License-Identifier: GPL-3.0-or-later
"""Command-line chat with Gloxi.

This is the developer front end for the 0.1 preview; the GTK window
uses the same ``Conversation``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from typing import TextIO

from gloxi import __version__
from gloxi.chat import Conversation
from gloxi.config import ROUTER_MODES, ConfigError, Settings, load_settings
from gloxi.llm import ChatClient, ModelError, ModelUnavailable
from gloxi.privacy import guard_factory
from gloxi.router import CloudNotAllowed, Route, Router, Target

HELP = (
    "Commands: /local <text> or /cloud <text> picks the model for one message, "
    "/reset clears the conversation, /exit quits."
)

LLAMA_HINT = (
    "Is llama-server running? For example:\n"
    "  llama-server -m model.gguf --port 8080\n"
    "or point Gloxi elsewhere with GLOXI_LOCAL_URL."
)

UNMASKED_WARNING = (
    "Note: masking is off (mask_cloud = false), so messages sent to the cloud are NOT masked."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gloxi", description="Chat with Gloxi.")
    parser.add_argument("--version", action="version", version=f"gloxi {__version__}")
    parser.add_argument("--url", help="local OpenAI-compatible endpoint (default: llama-server)")
    parser.add_argument("--model", help="local model name to request")
    parser.add_argument("--mode", choices=ROUTER_MODES, help="where messages go (default: auto)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("chat", help="interactive chat (default)")
    ask = sub.add_parser("ask", help="ask one question and exit")
    ask.add_argument("question", nargs="+")
    return parser


def split_command(text: str) -> tuple[Target | None, str]:
    """``/cloud hi`` → ("cloud", "hi"); anything else is left alone."""
    for target in ("local", "cloud"):
        prefix = f"/{target}"
        if text == prefix or text.startswith(prefix + " "):
            return target, text[len(prefix) :].strip()
    return None, text


def route_label(route: Route, router: Router) -> str:
    if route.target == "local":
        return "[local]"
    masked = "masked" if router.masks_cloud else "not masked"
    return f"[cloud: {route.destination}, {masked}]"


def _answer(
    conversation: Conversation, text: str, out: TextIO, *, force: Target | None = None
) -> bool:
    try:
        route = conversation.route(text, force=force)
        out.write(route_label(route, conversation.router) + " ")
        for piece in conversation.ask(text, route):
            out.write(piece)
            out.flush()
    except CloudNotAllowed as exc:
        print(f"\n{exc}", file=sys.stderr)
        return False
    except ModelUnavailable as exc:
        hint = LLAMA_HINT if conversation.last_route.target == "local" else ""
        print(f"\n{exc}\n{hint}".rstrip(), file=sys.stderr)
        return False
    except ModelError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return False
    out.write("\n")
    return True


def repl(
    conversation: Conversation,
    *,
    read: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
) -> int:
    print(f"Gloxi {__version__}. {HELP}", file=out)
    router = conversation.router
    if router.cloud_enabled:
        print(f"Cloud: on ({router.policy.mode} mode).", file=out)
        if not router.masks_cloud:
            print(UNMASKED_WARNING, file=out)
    else:
        print("Cloud: off. Everything stays on this computer.", file=out)
    while True:
        try:
            text = read("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=out)
            return 0
        if not text:
            continue
        if text in ("/exit", "/quit"):
            return 0
        if text == "/reset":
            conversation.reset()
            print("Conversation cleared.", file=out)
            continue
        if text == "/help":
            print(HELP, file=out)
            continue
        force, text = split_command(text)
        if not text:
            print(HELP, file=out)
            continue
        try:
            _answer(conversation, text, out, force=force)
        except KeyboardInterrupt:
            print("\n(interrupted)", file=out)


def _settings(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    overrides = {}
    if args.url:
        overrides["url"] = args.url.rstrip("/")
    if args.model:
        overrides["model"] = args.model
    if overrides:
        settings = replace(settings, local=replace(settings.local, **overrides))
    if args.mode:
        settings = replace(settings, router=replace(settings.router, mode=args.mode))
    return settings


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = _settings(args)
    except ConfigError as exc:
        print(f"gloxi: {exc}", file=sys.stderr)
        return 2

    with ExitStack() as stack:
        local = stack.enter_context(ChatClient(settings.local))
        cloud = stack.enter_context(ChatClient(settings.cloud)) if settings.cloud else None
        router = Router(local, cloud, settings.router, guard_factory=guard_factory(settings.router))
        conversation = Conversation(router, settings.system_prompt)
        if args.command == "ask":
            force, text = split_command(" ".join(args.question))
            return 0 if _answer(conversation, text, sys.stdout, force=force) else 1
        return repl(conversation)

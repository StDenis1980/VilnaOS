# SPDX-License-Identifier: GPL-3.0-or-later
"""Command-line chat with Gloxi.

This is the developer front end for the 0.1 preview; the GTK window
uses the same ``Conversation``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import replace
from typing import TextIO

from gloxi import __version__
from gloxi.chat import Conversation
from gloxi.config import ConfigError, Settings, load_settings
from gloxi.llm import ChatClient, ModelError, ModelUnavailable

HELP = "Commands: /reset clears the conversation, /exit quits."

LLAMA_HINT = (
    "Is llama-server running? For example:\n"
    "  llama-server -m model.gguf --port 8080\n"
    "or point Gloxi elsewhere with GLOXI_LOCAL_URL."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gloxi", description="Chat with Gloxi.")
    parser.add_argument("--version", action="version", version=f"gloxi {__version__}")
    parser.add_argument("--url", help="OpenAI-compatible endpoint (default: local llama-server)")
    parser.add_argument("--model", help="model name to request")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("chat", help="interactive chat (default)")
    ask = sub.add_parser("ask", help="ask one question and exit")
    ask.add_argument("question", nargs="+")
    return parser


def _answer(conversation: Conversation, text: str, out: TextIO) -> bool:
    try:
        for piece in conversation.ask(text):
            out.write(piece)
            out.flush()
    except ModelUnavailable as exc:
        print(f"\n{exc}\n{LLAMA_HINT}", file=sys.stderr)
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
        try:
            _answer(conversation, text, out)
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
    return settings


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = _settings(args)
    except ConfigError as exc:
        print(f"gloxi: {exc}", file=sys.stderr)
        return 2

    with ChatClient(settings.local) as client:
        conversation = Conversation(client, settings.system_prompt)
        if args.command == "ask":
            return 0 if _answer(conversation, " ".join(args.question), sys.stdout) else 1
        return repl(conversation)

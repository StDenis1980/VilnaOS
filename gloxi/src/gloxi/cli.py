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
from gettext import gettext as _
from typing import TextIO

from gloxi import __version__
from gloxi.backup_intro import BackupIntro, backup_intro
from gloxi.chat import Conversation
from gloxi.config import ROUTER_MODES, ConfigError, Settings, load_settings
from gloxi.engine import Engine, EngineError, Plan, download, human_size, plan
from gloxi.i18n import N_, setup
from gloxi.llm import ChatClient, ModelError, ModelUnavailable
from gloxi.privacy import guard_factory
from gloxi.router import CloudNotAllowed, Route, Router, Target
from gloxi.tools import ToolOutcome, file_tools

HELP = N_(
    "Commands: /local <text> or /cloud <text> picks the model for one message, "
    "/reset clears the conversation, /backup sets up backups, /exit quits."
)

LLAMA_HINT = N_(
    "Is llama-server running? For example:\n"
    "  llama-server -m model.gguf --port 8080\n"
    "or point Gloxi elsewhere with GLOXI_LOCAL_URL."
)

UNMASKED_WARNING = N_(
    "Note: masking is off (mask_cloud = false), so messages sent to the cloud are NOT masked."
)


MODEL_CONSENT = N_(
    "Gloxi needs a language model on this computer to answer. It is {name}, "
    "{size}, licence {licence}, from {source}. It will be kept in {folder}. "
    "Only the file is downloaded: nothing about you is sent."
)

NO_MODEL_HINT = N_("There is no language model yet. Run gloxi in a terminal once to download it.")

YES = {"y", "yes", "т", "так"}


def model_consent_text(p: Plan) -> str:
    return _(MODEL_CONSENT).format(
        name=p.model.name,
        size=human_size(p.model.size),
        licence=p.model.licence,
        source=p.model.source,
        folder=p.folder,
    )


def prepare_engine(
    settings: Settings,
    *,
    read: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
    interactive: bool = True,
    planner: Callable[..., Plan] = plan,
    fetch: Callable[..., object] = download,
    engine_type: Callable[..., Engine] = Engine,
) -> Engine | None:
    """Start the bundled model if needed; download it first only after a yes."""
    p = planner(settings.local.url, start_server=settings.start_server)
    if p.action in ("ready", "none"):
        return None
    model_path = p.model_path
    if p.action == "download":
        if not interactive:
            print(_(NO_MODEL_HINT), file=out)
            return None
        print(model_consent_text(p), file=out)
        try:
            answer = read(_("Download it now? [y/N] ")).strip().casefold()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in YES | {_("yes").casefold()}:
            print(_("Not downloaded. Gloxi will ask again next time."), file=out)
            return None
        last = -1

        def progress(done: int, total: int) -> None:
            nonlocal last
            percent = done * 100 // max(total, 1)
            if percent != last:
                last = percent
                out.write("\r" + _("Downloading the model: {percent}%").format(percent=percent))
                out.flush()

        try:
            model_path = fetch(p.model, p.folder, progress=progress)
        except (EngineError, KeyboardInterrupt) as exc:
            print("\n" + _("The model was not downloaded: {why}").format(why=exc), file=out)
            return None
        print("\n" + _("The model is ready."), file=out)
    print(_("Starting the model on this computer…"), file=out)
    engine = engine_type(p.binary, model_path, settings.local.url)
    try:
        engine.start()
    except EngineError as exc:
        print(_("The model could not be started: {why}").format(why=exc), file=out)
        return None
    return engine


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
        return _("[local]")
    if router.masks_cloud:
        return _("[cloud: {host}, masked]").format(host=route.destination)
    return _("[cloud: {host}, not masked]").format(host=route.destination)


def tool_note(outcome: ToolOutcome) -> str:
    return f"\n[{outcome.summary()}]\n"


def _answer(
    conversation: Conversation, text: str, out: TextIO, *, force: Target | None = None
) -> bool:
    conversation.on_tool = lambda outcome: out.write(tool_note(outcome))
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
        hint = _(LLAMA_HINT) if conversation.last_route.target == "local" else ""
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
    intro: BackupIntro | None = None,
) -> int:
    print(f"Gloxi {__version__}. {_(HELP)}", file=out)
    router = conversation.router
    if router.cloud_enabled:
        print(_("Cloud: on ({mode} mode).").format(mode=router.policy.mode), file=out)
        if not router.masks_cloud:
            print(_(UNMASKED_WARNING), file=out)
    else:
        print(_("Cloud: off. Everything stays on this computer."), file=out)
    if intro and intro.due():
        run_intro(intro, read, out)
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
            print(_("Conversation cleared."), file=out)
            continue
        if text == "/backup":
            if intro:
                run_intro(intro, read, out)
            else:
                print(_("Backups need Herkos, and it is not running."), file=out)
            continue
        if text == "/help":
            print(_(HELP), file=out)
            continue
        force, text = split_command(text)
        if not text:
            print(_(HELP), file=out)
            continue
        try:
            _answer(conversation, text, out, force=force)
        except KeyboardInterrupt:
            print("\n" + _("(interrupted)"), file=out)


def run_intro(intro: BackupIntro, read: Callable[[str], str], out: TextIO) -> None:
    """The backup talk (design.md 2a) in the terminal; Ctrl+D leaves it."""
    turn = intro.start()
    while turn is not None:
        print(f"\nGloxi: {turn.render()}", file=out)
        if turn.done:
            break
        try:
            text = read("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=out)
            break
        turn = intro.answer(text)
    print(file=out)


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
    setup()
    args = build_parser().parse_args(argv)
    try:
        settings = _settings(args)
    except ConfigError as exc:
        print(f"gloxi: {exc}", file=sys.stderr)
        return 2

    with ExitStack() as stack:
        engine = prepare_engine(settings, interactive=sys.stdin.isatty())
        if engine:
            stack.callback(engine.stop)
        local = stack.enter_context(ChatClient(settings.local))
        cloud = stack.enter_context(ChatClient(settings.cloud)) if settings.cloud else None
        router = Router(local, cloud, settings.router, guard_factory=guard_factory(settings.router))
        tools = file_tools(settings.tools_enabled)
        if tools:
            stack.callback(tools.close)
        conversation = Conversation(router, settings.system_prompt, tools=tools)
        if args.command == "ask":
            force, text = split_command(" ".join(args.question))
            return 0 if _answer(conversation, text, sys.stdout, force=force) else 1
        intro = backup_intro(settings.tools_enabled)
        if intro:
            stack.callback(intro.close)
        return repl(conversation, intro=intro)

# SPDX-License-Identifier: GPL-3.0-or-later
"""Command-line chat with Gloxi.

This is the developer front end for the 0.1 preview; the GTK window
uses the same ``Conversation``.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
import webbrowser
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from gettext import gettext as _
from typing import TextIO

import httpx

from gloxi import __version__, check, crash, upgrade
from gloxi.backup_intro import BackupIntro, Turn, backup_intro
from gloxi.chat import Conversation
from gloxi.config import ROUTER_MODES, ConfigError, Settings, load_settings
from gloxi.engine import (
    DECIMAL_POINT,
    LARGE_MODEL,
    Engine,
    EngineError,
    Plan,
    download,
    human_size,
    model_name,
    plan,
    total_ram,
)
from gloxi.feedback import FeedbackTalk, wants_feedback
from gloxi.herkos_local import start_if_needed
from gloxi.i18n import N_, setup
from gloxi.llm import ChatClient, ModelError, ModelUnavailable
from gloxi.privacy import guard_factory
from gloxi.router import CloudNotAllowed, Route, Router, Target
from gloxi.tools import ToolOutcome, file_tools
from gloxi.upgrade import SpeedResult, measure_speed

HELP = N_(
    "Commands: /local <text> or /cloud <text> picks the model for one message, "
    "/reset clears the conversation, /backup sets up backups, /feedback reports a problem "
    "or suggests something, /exit quits."
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

UPGRADE_CONSENT = N_(
    "This computer is fast enough for a better model: {name}, {size}, licence {licence}, "
    "from {source}. It answers better, in Ukrainian too; Gloxi expects about {speed} "
    "tokens per second here. It will be kept in {folder}, next to the current model, "
    "and Gloxi will use it from the next start. Only the file is downloaded: nothing "
    "about you is sent."
)

NO_MODEL_HINT = N_("There is no language model yet. Run gloxi in a terminal once to download it.")

NO_MODEL_EXIT = N_(
    "Without a language model Gloxi cannot answer. Run gloxi again when you want to download it."
)

YES = {"y", "yes", "т", "так"}


def yes_words() -> set[str]:
    """What counts as yes: English, Ukrainian, and the translated word with its first letter
    (the prompt shows that letter, e.g. "[j/N]" in German)."""
    word = _("yes").casefold()
    return YES | {word, word[:1]}


class NoModel(Exception):
    """The model is needed but missing: declined, failed to download or to start."""


def model_consent_text(p: Plan) -> str:
    return _(MODEL_CONSENT).format(
        name=p.model.name,
        size=human_size(p.model.size),
        licence=p.model.licence,
        source=_(p.model.source),
        folder=p.folder,
    )


def upgrade_consent_text(folder, expected: float) -> str:
    return _(UPGRADE_CONSENT).format(
        name=LARGE_MODEL.name,
        size=human_size(LARGE_MODEL.size),
        licence=LARGE_MODEL.licence,
        source=_(LARGE_MODEL.source),
        speed=human_number(expected),
        folder=folder,
    )


def human_number(value: float) -> str:
    return f"{value:.1f}".replace(".", _(DECIMAL_POINT))


def prepare_engine(
    settings: Settings,
    *,
    read: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
    interactive: bool = True,
    planner: Callable[..., Plan] = plan,
    fetch: Callable[..., object] = download,
    engine_type: Callable[..., Engine] = Engine,
    ram: int | None | Callable[[], int | None] = total_ram,
    measure: Callable[..., SpeedResult] = measure_speed,
) -> Engine | None:
    """Start the bundled model if needed; download it first only after a yes.

    On a 16 GB computer, once, the speed test may then offer the larger model.

    Raises ``NoModel`` if the model is needed and still missing afterwards.
    """
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
        if answer not in yes_words():
            print(_("Not downloaded. Gloxi will ask again next time."), file=out)
            raise NoModel
        try:
            model_path = fetch(p.model, p.folder, progress=_percent_printer(out))
        except (EngineError, KeyboardInterrupt) as exc:
            print("\n" + _("The model was not downloaded: {why}").format(why=exc), file=out)
            raise NoModel from None
        print("\n" + _("The model is ready."), file=out)
    print(_("Starting {name} on this computer…").format(name=model_name(model_path)), file=out)
    engine = engine_type(p.binary, model_path, settings.local.url)
    try:
        engine.start()
    except EngineError as exc:
        print(_("The model could not be started: {why}").format(why=exc), file=out)
        raise NoModel from None
    if interactive:
        _offer_large_model(p, settings, ram() if callable(ram) else ram, read, out, fetch, measure)
    return engine


def _offer_large_model(p, settings, ram, read, out, fetch, measure) -> None:
    """The speed test of gloxi.upgrade and, if it says so, the question."""
    if upgrade.needs_test(p, ram):
        print(_("Checking how fast this computer runs the model (about a minute)…"), file=out)
        try:
            upgrade.record_result(p.folder, measure(settings.local.url))
        except (httpx.HTTPError, ValueError, OSError):
            return  # try again next time; the chat must not wait for this
    if not upgrade.should_offer(p.folder):
        return
    expected = upgrade.load_state(p.folder).get("large_expected", 0.0)
    print(upgrade_consent_text(p.folder, expected), file=out)
    try:
        answer = read(_("Download it now? [y/N] ")).strip().casefold()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    yes = answer in yes_words()
    upgrade.record_answer(p.folder, yes)
    if not yes:
        print(_("Gloxi keeps the current model and will not ask again."), file=out)
        return
    try:
        fetch(LARGE_MODEL, p.folder, progress=_percent_printer(out))
    except (EngineError, KeyboardInterrupt) as exc:
        print("\n" + _("The model was not downloaded: {why}").format(why=exc), file=out)
        upgrade.record_answer_reset(p.folder)
        return
    print("\n" + _("The model is ready. Gloxi will use it from the next start."), file=out)


def _percent_printer(out):
    last = -1

    def progress(done: int, total: int) -> None:
        nonlocal last
        percent = done * 100 // max(total, 1)
        if percent != last:
            last = percent
            out.write("\r" + _("Downloading the model: {percent}%").format(percent=percent))
            out.flush()

    return progress


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
    sub.add_parser("check", help="will this computer run the local model well?")
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
    feedback: FeedbackTalk | None = None,
    open_url: Callable[[str], object] = webbrowser.open,
) -> int:
    feedback = feedback or FeedbackTalk()
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
        if text == "/feedback" or text.startswith("/feedback ") or wants_feedback(text):
            first = text.removeprefix("/feedback") if text.startswith("/feedback") else ""
            run_feedback(feedback, feedback.start(first), read, out, open_url)
            continue
        force, text = split_command(text)
        if not text:
            print(_(HELP), file=out)
            continue
        try:
            _answer(conversation, text, out, force=force)
        except KeyboardInterrupt:
            print("\n" + _("(interrupted)"), file=out)


def run_feedback(
    talk: FeedbackTalk,
    turn: Turn,
    read: Callable[[str], str],
    out: TextIO,
    open_url: Callable[[str], object],
) -> None:
    """``/feedback`` in the terminal (ADR 0018); Ctrl+D or /cancel leaves it."""
    while True:
        print(f"\nGloxi: {turn.render()}", file=out)
        if turn.done:
            break
        try:
            text = read("> ").strip()
        except (EOFError, KeyboardInterrupt):
            text = "/cancel"
        turn = talk.cancel() if text == "/cancel" else talk.answer(text)
    if talk.url:
        # No browser: the address is already on the screen.
        with contextlib.suppress(Exception):
            open_url(talk.url)
    print(file=out)


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


def _leave(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    setup()
    crash.install()
    args = build_parser().parse_args(argv)
    if args.command == "check":
        print(check.collect().render())
        return 0
    try:
        settings = _settings(args)
    except ConfigError as exc:
        print(f"gloxi: {exc}", file=sys.stderr)
        return 2

    # Closing the terminal (SIGHUP) or `kill` (SIGTERM) must stop the model
    # and Herkos too: leave through the ExitStack below, as Ctrl+C does.
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _leave)

    with ExitStack() as stack:
        try:
            engine = prepare_engine(settings, interactive=sys.stdin.isatty())
        except NoModel:
            if not settings.cloud:
                print(_(NO_MODEL_EXIT))
                return 1
            engine = None  # the cloud still answers
        if engine:
            stack.callback(engine.stop)
        local = stack.enter_context(ChatClient(settings.local))
        cloud = stack.enter_context(ChatClient(settings.cloud)) if settings.cloud else None
        router = Router(local, cloud, settings.router, guard_factory=guard_factory(settings.router))
        tty = sys.stdin.fileno() if sys.stdin.isatty() else None
        herkos = start_if_needed(settings.tools_enabled, confirm_fd=tty)
        if herkos:
            stack.callback(herkos.stop)
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

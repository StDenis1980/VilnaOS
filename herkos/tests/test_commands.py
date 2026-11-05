# SPDX-License-Identifier: GPL-3.0-or-later
"""Levels of a program's own commands (ADR 0017, stage 2, step 7).

A generic operation such as "execute" runs any command of the program. Its
level is the level of the riskiest command it runs, from the manifest's
[commands]; a command the manifest does not name is R4.
"""

import tomllib

import pytest

from herkos.manifest import MAX_CALL_COMMANDS, ManifestError, parse_manifest
from herkos.policy import SessionState, assess
from herkos.request import parse_request
from herkos.risk import Level

APP = """
tool = "org.example.writer"

[commands]
"text.insert" = "R1"
"file.save" = "R2"
"file.print" = "forbidden"
"view.*" = "R0"
"file.*" = "R3"

[operations.execute]
level = "R1"
command_arg = "command"

[operations.batch]
level = "R1"
command_arg = "commands"
command_key = "command"

[operations.get_text]
level = "R1"
"""


def manifest(text=APP):
    return parse_manifest(tomllib.loads(text))


def run(operation, arguments, *, session=None, m=None):
    request = parse_request(
        {
            "tool": "org.example.writer",
            "operation": operation,
            "reason": "test",
            "arguments": arguments,
        }
    )
    return assess(
        request,
        m or manifest(),
        session or SessionState(),
        allowed_roots=(),
        bulk_threshold=20,
    )


def test_an_id_wins_over_its_group_and_the_nearest_group_wins():
    m = manifest()
    assert m.command_level("file.save") == (True, Level.R2)
    assert m.command_level("file.export.pdf") == (True, Level.R3)
    assert m.command_level("view.zoom") == (True, Level.R0)
    assert m.command_level("file.print") == (True, None)
    assert m.command_level("table.insert") == (False, None)
    assert m.command_level("view") == (False, None)


def test_execute_is_as_risky_as_its_command():
    assert run("execute", {"command": "text.insert"}).level == Level.R1
    # R2 that Herkos cannot undo becomes R3, as for any other operation.
    a = run("execute", {"command": "file.save"})
    assert a.base == Level.R2 and a.level == Level.R3
    # The operation's own level is the floor.
    assert run("execute", {"command": "view.zoom"}).level == Level.R1


def test_a_command_the_manifest_does_not_name_is_r4():
    a = run("execute", {"command": "table.insert"})
    assert a.level == Level.R4
    assert any("table.insert" in r for r in a.raises)


def test_a_forbidden_command_is_never_allowed():
    assert run("execute", {"command": "file.print"}).forbidden
    batch = [{"command": "text.insert"}, {"command": "file.print"}]
    assert run("batch", {"commands": batch}).forbidden


def test_a_batch_is_as_risky_as_its_riskiest_command():
    steps = [{"command": "text.insert"}, {"command": "view.zoom"}]
    assert run("batch", {"commands": steps}).level == Level.R1
    steps.append({"command": "file.save"})
    assert run("batch", {"commands": steps}).base == Level.R2
    steps.append({"command": "table.insert"})
    assert run("batch", {"commands": steps}).level == Level.R4


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"command": ""},
        {"command": 7},
        {"command": "view.*"},
        {"command": "../x"},
    ],
)
def test_a_call_whose_command_cannot_be_read_is_r4(arguments):
    a = run("execute", arguments)
    assert a.level == Level.R4 and a.raises


@pytest.mark.parametrize(
    "commands",
    [
        [],
        "text.insert",
        [{"cmd": "text.insert"}],
        [{"command": "text.insert"}] * (MAX_CALL_COMMANDS + 1),
    ],
)
def test_a_batch_that_cannot_be_read_is_r4(commands):
    if isinstance(commands, str):
        # One id instead of a list is still one command.
        assert run("batch", {"commands": commands}).level == Level.R1
        return
    assert run("batch", {"commands": commands}).level == Level.R4


def test_other_raises_still_apply():
    s = SessionState()
    s.taint("mail")
    a = run("execute", {"command": "file.save"}, session=s)
    assert a.level == Level.R3
    assert run("execute", {"command": "text.insert"}, session=s).level == Level.R1


@pytest.mark.parametrize(
    "text",
    [
        'tool = "a"\n[operations.x]\nlevel = "R1"\ncommand_arg = "command"',
        'tool = "a"\n[commands]\n"a.*.*" = "R1"\n[operations.x]\nlevel = "R1"',
        'tool = "a"\n[commands]\n"*" = "R1"\n[operations.x]\nlevel = "R1"',
        'tool = "a"\n[commands]\n"a b" = "R1"\n[operations.x]\nlevel = "R1"',
        'tool = "a"\n[commands]\n"a" = "R9"\n[operations.x]\nlevel = "R1"',
        'tool = "a"\n[commands]\n"a" = 1\n[operations.x]\nlevel = "R1"',
        'tool = "a"\ncommands = {}\n[operations.x]\nlevel = "R1"',
        'tool = "a"\n[commands]\n"a" = "R1"\n[operations.x]\nlevel = "R1"\ncommand_key = "id"',
        'tool = "a"\n[commands]\n"a" = "R1"\n[operations.x]\nlevel = "R1"\ncommand_arg = "A B"',
    ],
)
def test_bad_command_tables_are_refused(text):
    with pytest.raises(ManifestError):
        parse_manifest(tomllib.loads(text))

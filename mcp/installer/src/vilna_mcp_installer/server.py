# SPDX-License-Identifier: Apache-2.0
"""MCP server of the installer over stdio (newline-delimited JSON-RPC 2.0).

    vilna-mcp-installer --plan PATH [--demo empty|windows] [--stage alpha|beta]

It reads the computer (hardware, disks, Wi-Fi networks, keyboards, time
zones) and changes **only the plan** (installer.md, section 2). There is no
tool that erases, partitions, connects or installs: erasing is only ever the
result of a plan the person confirmed on the Herkos screen.

Function names use ``_`` instead of the ``.`` of installer.md
(``disks_survey`` for ``disks.survey``), because OpenAI-style function
calling, which Gloxi uses with the local model, does not take dots.

Whatever the person alone may set (secrets entered, the recovery key saved,
the confirmation) is not in any tool's arguments, and arguments a tool does
not list are refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from vilna_mcp_installer import __version__
from vilna_mcp_installer import plan as plans
from vilna_mcp_installer.machine import (
    KEYBOARD_OF,
    LOCALE_OF,
    TIMEZONE_OF,
    DemoMachine,
    Machine,
    MachineError,
    survey,
    timezones,
)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_LISTED = 30

_STR = {"type": "string"}
_READ = {"readOnlyHint": True}
_PLAN = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}


def _tool(name: str, description: str, props: dict, required: list[str], **annotations: Any):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": annotations,
    }


def _list(item: dict, description: str) -> dict:
    return {"type": "array", "items": item, "description": description}


TOOLS = [
    _tool(
        "hardware_describe",
        "Processor, memory, UEFI, Secure Boot and TPM of this computer",
        {},
        [],
        **_READ,
    ),
    _tool(
        "locale_list",
        "Languages Vilna is translated into, with the keyboard and time zone each starts with",
        {},
        [],
        **_READ,
    ),
    _tool(
        "keyboard_list",
        "Keyboard layouts (xkb codes) whose code or name contains the query",
        {"query": _STR},
        [],
        **_READ,
    ),
    _tool(
        "timezone_list",
        "Time zones (Area/City) that contain the query, such as 'Kyiv' or 'Europe'",
        {"query": _STR},
        ["query"],
        **_READ,
    ),
    _tool(
        "network_scan", "Wi-Fi networks nearby and whether a cable is connected", {}, [], **_READ
    ),
    _tool(
        "disks_survey",
        "Disks, partitions, systems found on them, encryption and free space",
        {},
        [],
        **_READ,
    ),
    _tool("plan_get", "The installation plan as it is now", {}, [], **_READ),
    _tool(
        "plan_set_languages",
        "Set the system languages (locales, the main one first) and keyboard layouts",
        {
            "languages": _list(_STR, "Locales such as uk_UA.UTF-8, en_US.UTF-8"),
            "keyboards": _list(_STR, "xkb layouts such as ua, us, pl"),
        },
        ["languages", "keyboards"],
        **_PLAN,
    ),
    _tool(
        "plan_set_network",
        "Install offline, over the cable, or over a Wi-Fi network. The person types a "
        "Wi-Fi password into a protected field; never ask for it in the chat",
        {
            "mode": {"type": "string", "enum": list(plans.NETWORK)},
            "ssid": {"type": "string", "description": "Name of the Wi-Fi network"},
        },
        ["mode"],
        **_PLAN,
    ),
    _tool(
        "plan_set_disk",
        "Choose the disk (its id from disks_survey) and what to do with it. 'replace' "
        "erases the whole disk; 'manual' opens the partition editor",
        {
            "target": _STR,
            "layout": {"type": "string", "enum": list(plans.LAYOUTS)},
        },
        ["target", "layout"],
        **_PLAN,
    ),
    _tool(
        "plan_set_encryption",
        "Encrypt the new system: tpm2 (recommended, with a recovery key), passphrase, or none",
        {"method": {"type": "string", "enum": list(plans.ENCRYPTION)}},
        ["method"],
        **_PLAN,
    ),
    _tool(
        "plan_set_user",
        "The person's full name, a login name (lower-case Latin) and the computer's name. "
        "The password goes into a protected field; never ask for it in the chat",
        {"full_name": _STR, "username": _STR, "hostname": _STR},
        ["full_name", "username", "hostname"],
        **_PLAN,
    ),
    _tool(
        "plan_set_time",
        "Set the time zone (Area/City) and the region for date and number formats (a locale)",
        {"timezone": _STR, "region": _STR},
        ["timezone", "region"],
        **_PLAN,
    ),
    _tool(
        "plan_validate",
        "Check the plan: what is missing, what blocks installing, and every change to the disk",
        {},
        [],
        **_READ,
    ),
]
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}
_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
}


def check_arguments(name: str, args: Any) -> dict[str, Any]:
    schema = _SCHEMAS[name]
    if not isinstance(args, dict):
        raise plans.PlanError("arguments must be an object")
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise plans.PlanError(
            f"unknown arguments: {sorted(unknown)}; allowed: {sorted(schema['properties'])}"
        )
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        raise plans.PlanError(f"missing arguments: {missing}")
    for k, v in args.items():
        prop = schema["properties"][k]
        if not _TYPES[prop["type"]](v):
            raise plans.PlanError(f"'{k}' must be a {prop['type']}")
        if "enum" in prop and v not in prop["enum"]:
            raise plans.PlanError(f"'{k}' must be one of {prop['enum']}")
    return args


class Installer:
    """The tools, over one plan file and one computer."""

    def __init__(self, plan_path: Path, machine: Machine, *, stage: str = "alpha") -> None:
        self.plan_path = plan_path
        self.machine = machine
        self.stage = stage
        self._survey: dict[str, Any] | None = None
        self._networks: list[dict[str, Any]] = []

    # reading

    def hardware_describe(self) -> dict[str, Any]:
        return self.machine.hardware()

    def locale_list(self) -> dict[str, Any]:
        return {
            "languages": [
                {
                    "language": code,
                    "locale": loc,
                    "keyboard": KEYBOARD_OF[code],
                    "timezone": TIMEZONE_OF.get(loc.split(".")[0]),
                }
                for code, loc in LOCALE_OF.items()
            ],
            "note": "Other locales work too; Vilna's own programs are then in English.",
        }

    def keyboard_list(self, query: str = "") -> dict[str, Any]:
        q = query.strip().casefold()
        rows = [
            k
            for k in self.machine.keyboards()
            if not q or q in k["code"].casefold() or q in k["name"].casefold()
        ]
        return {"keyboards": rows[:MAX_LISTED], "more": max(len(rows) - MAX_LISTED, 0)}

    def timezone_list(self, query: str) -> dict[str, Any]:
        q = query.strip().casefold().replace(" ", "_")
        rows = [z for z in timezones() if q and q in z.casefold()]
        return {"timezones": rows[:MAX_LISTED], "more": max(len(rows) - MAX_LISTED, 0)}

    def network_scan(self) -> dict[str, Any]:
        try:
            self._networks = self.machine.networks()
        except MachineError as e:
            return {"wifi": [], "wired": self.machine.wired(), "error": str(e)}
        return {"wifi": self._networks, "wired": self.machine.wired()}

    def disks_survey(self) -> dict[str, Any]:
        self._survey = survey(self.machine.disks())
        return self._survey

    def plan_get(self) -> dict[str, Any]:
        return plans.public(plans.load(self.plan_path))

    def plan_validate(self) -> dict[str, Any]:
        # Always against the disks as they are now, not as the model last saw them.
        return plans.validate(plans.load(self.plan_path), self.disks_survey(), stage=self.stage)

    # changing the plan

    def _change(self, setter: str, **kwargs: Any) -> dict[str, Any]:
        plan = plans.load(self.plan_path)
        changed = plans.SETTERS[setter](plan, **kwargs)
        plans.save(plan, self.plan_path)
        return {"changed": changed, "missing": plans.validate(plan, self._last_survey())["missing"]}

    def _last_survey(self) -> dict[str, Any]:
        return self._survey if self._survey is not None else self.disks_survey()

    def plan_set_languages(self, languages: list[str], keyboards: list[str]) -> dict[str, Any]:
        return self._change("set_languages", languages=languages, keyboards=keyboards)

    def plan_set_network(self, mode: str, ssid: str | None = None) -> dict[str, Any]:
        secured = False
        if mode == "wifi":
            if not self._networks:
                self.network_scan()
            known = {n["ssid"]: n for n in self._networks}
            if ssid not in known:
                raise plans.PlanError(f"no Wi-Fi network {ssid!r} nearby; networks: {list(known)}")
            secured = bool(known[ssid]["secured"])
        out = self._change("set_network", mode=mode, ssid=ssid, secured=secured)
        if out["changed"].get("secret") == plans.NEEDED:
            out["next"] = (
                "The installer now shows a protected field for the password. "
                "Tell the person to type it there; you will not see it."
            )
        return out

    def plan_set_disk(self, target: str, layout: str) -> dict[str, Any]:
        allowed = plans.STAGE_LAYOUTS.get(self.stage, plans.LAYOUTS)
        if layout not in allowed:
            raise plans.PlanError(
                f"'{layout}' comes in a later version of the installer; now: {list(allowed)}"
            )
        out = self._change("set_disk", target=target, layout=layout, survey=self._last_survey())
        if layout == "manual":
            out["next"] = "The installer opens the partition editor for the person."
        return out

    def plan_set_encryption(self, method: str) -> dict[str, Any]:
        out = self._change("set_encryption", method=method)
        if method != "none":
            out["next"] = (
                "The recovery key is shown to the person before installing, on the "
                "confirmation screen; explain why to keep it somewhere safe."
            )
        return out

    def plan_set_user(self, full_name: str, username: str, hostname: str) -> dict[str, Any]:
        out = self._change("set_user", full_name=full_name, username=username, hostname=hostname)
        if out["changed"].get("password") == plans.NEEDED:
            out["next"] = (
                "The installer now shows a protected field for the password. "
                "Tell the person to type it there; you will not see it."
            )
        return out

    def plan_set_time(self, timezone: str, region: str) -> dict[str, Any]:
        return self._change("set_time", timezone=timezone, region=region)


class Server:
    def __init__(self, installer: Installer) -> None:
        self.installer = installer

    def handle(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "not a JSON-RPC 2.0 message")
        method, id_ = msg.get("method"), msg.get("id")
        if "id" not in msg:
            return None
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return _error(id_, -32602, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                id_,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "vilna-mcp-installer", "version": __version__},
                },
            )
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            return _result(id_, {"tools": TOOLS})
        if method == "tools/call":
            return _result(id_, self.call(params.get("name"), params.get("arguments") or {}))
        return _error(id_, -32601, f"unknown method {method}")

    def call(self, name: Any, args: Any) -> dict:
        if name not in _SCHEMAS:
            return _tool_error(f"unknown tool {name}; tools: {sorted(_SCHEMAS)}")
        try:
            out = getattr(self.installer, name)(**check_arguments(name, args))
        except (plans.PlanError, MachineError) as e:
            return _tool_error(str(e))
        except Exception as e:  # report and keep serving: the conversation must go on
            return _tool_error(f"{type(e).__name__}: {e}")
        return {
            "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out,
            "isError": False,
        }

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                response = _error(None, -32700, "not valid JSON")
            else:
                response = self.handle(msg)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


def _result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vilna-mcp-installer")
    p.add_argument("--plan", type=Path, required=True, help="the plan file to fill")
    p.add_argument("--demo", choices=("empty", "windows"), help="a synthetic computer")
    p.add_argument("--stage", choices=tuple(plans.STAGE_LAYOUTS), default="alpha")
    args = p.parse_args(argv)
    machine = DemoMachine(args.demo) if args.demo else Machine()
    Server(Installer(args.plan, machine, stage=args.stage)).serve(sys.stdin, sys.stdout)
    return 0

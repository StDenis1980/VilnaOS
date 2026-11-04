// SPDX-License-Identifier: GPL-3.0-or-later
//
// A small D-Bus service so Gloxi can change its own settings in the Shell.
// Gloxi runs in a Flatpak and cannot write this extension's GSettings; with
// `--talk-name=io.github.stdenis1980.VilnaShell` it can set these
// properties. Nothing here reads or changes anything beyond the settings
// below, so any program in the session may use it.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

export const BUS_NAME = 'io.github.stdenis1980.VilnaShell';
export const OBJECT_PATH = '/io/github/stdenis1980/VilnaShell';
export const INTERFACE_VERSION = 1;

const INTERFACE_XML = `
<node>
  <interface name="io.github.stdenis1980.VilnaShell1">
    <property name="Version" type="u" access="read"/>
    <property name="PasswordLatinLayout" type="b" access="read"/>
    <property name="GloxiKeepAbove" type="b" access="readwrite"/>
    <property name="GloxiDock" type="b" access="readwrite"/>
  </interface>
</node>`;

// D-Bus property → [GSettings key, writable]. Only the person turns the
// password layout off (in the settings), never another program.
const PROPERTIES = {
    PasswordLatinLayout: ['password-latin-layout', false],
    GloxiKeepAbove: ['gloxi-keep-above', true],
    GloxiDock: ['gloxi-dock', true],
};

export class VilnaShellService {
    constructor(settings) {
        this._settings = settings;
        this._signals = [];
        this._exported = null;
        this._nameId = 0;
    }

    enable() {
        const object = {
            get Version() {
                return INTERFACE_VERSION;
            },
        };
        for (const [property, [key, writable]] of Object.entries(PROPERTIES)) {
            Object.defineProperty(object, property, {
                get: () => this._settings.get_boolean(key),
                set: value => {
                    if (writable)
                        this._settings.set_boolean(key, Boolean(value));
                },
                enumerable: true,
            });
            this._signals.push(this._settings.connect(`changed::${key}`, () =>
                this._exported?.emit_property_changed(
                    property, GLib.Variant.new_boolean(this._settings.get_boolean(key)))));
        }
        this._exported = Gio.DBusExportedObject.wrapJSObject(INTERFACE_XML, object);
        this._exported.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.NONE, null, null);
    }

    disable() {
        for (const id of this._signals)
            this._settings.disconnect(id);
        this._signals = [];
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = 0;
        }
        this._exported?.unexport();
        this._exported = null;
    }
}

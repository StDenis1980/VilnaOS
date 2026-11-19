// SPDX-License-Identifier: GPL-3.0-or-later
//
// A small D-Bus service so Gloxi can change its own settings in the Shell.
// Gloxi runs in a Flatpak and cannot write this extension's GSettings; with
// `--talk-name=io.github.stdenis1980.VilnaShell` it can set these
// properties. Nothing here reads or changes anything beyond the settings
// below, so any program in the session may use it.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {PANEL_SIZES} from './lib/geometry.js';

export const BUS_NAME = 'io.github.stdenis1980.VilnaShell';
export const OBJECT_PATH = '/io/github/stdenis1980/VilnaShell';
// 2: GloxiPanelEdge and GloxiPanelSize (stage A of the Gloxi window).
export const INTERFACE_VERSION = 2;

const INTERFACE_XML = `
<node>
  <interface name="io.github.stdenis1980.VilnaShell1">
    <property name="Version" type="u" access="read"/>
    <property name="PasswordLatinLayout" type="b" access="read"/>
    <property name="GloxiKeepAbove" type="b" access="readwrite"/>
    <property name="GloxiDock" type="b" access="readwrite"/>
    <property name="GloxiPanelEdge" type="b" access="readwrite"/>
    <property name="GloxiPanelSize" type="s" access="readwrite"/>
  </interface>
</node>`;

// D-Bus property → [GSettings key, writable, type]. Only the person turns the
// password layout off (in the settings), never another program.
const PROPERTIES = {
    PasswordLatinLayout: ['password-latin-layout', false, 'b'],
    GloxiKeepAbove: ['gloxi-keep-above', true, 'b'],
    GloxiDock: ['gloxi-dock', true, 'b'],
    GloxiPanelEdge: ['gloxi-panel-edge', true, 'b'],
    // The one string: anything but the schema's sizes is ignored.
    GloxiPanelSize: ['gloxi-panel-size', true, 's'],
};

function read(settings, key, type) {
    return type === 's' ? settings.get_string(key) : settings.get_boolean(key);
}

function write(settings, key, type, value) {
    if (type === 'b') {
        settings.set_boolean(key, Boolean(value));
        return;
    }
    if (typeof value === 'string' && PANEL_SIZES.includes(value))
        settings.set_string(key, value);
}

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
        for (const [property, [key, writable, type]] of Object.entries(PROPERTIES)) {
            Object.defineProperty(object, property, {
                get: () => read(this._settings, key, type),
                set: value => {
                    if (writable)
                        write(this._settings, key, type, value);
                },
                enumerable: true,
            });
            this._signals.push(this._settings.connect(`changed::${key}`, () =>
                this._exported?.emit_property_changed(
                    property, new GLib.Variant(type, read(this._settings, key, type)))));
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

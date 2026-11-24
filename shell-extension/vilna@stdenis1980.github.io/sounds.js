// SPDX-License-Identifier: GPL-3.0-or-later
//
// Sound theme and sounds turned off one by one (lib/soundTheme.js).
//
// Reads sound-theme and disabled-sounds from the extension settings, which
// the preferences and Gloxi (vilna-mcp-settings) change, and keeps the
// person's theme ~/.local/share/sounds/vilna-user in step with them; then
// points GNOME at it. All sounds off is GNOME's event-sounds switch, which
// this leaves alone.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {USER_THEME, plan} from './lib/soundTheme.js';

const SOUND_SCHEMA = 'org.gnome.desktop.sound';

export class Sounds {
    constructor(settings) {
        this._settings = settings;
        this._signals = [];
    }

    enable() {
        this._sound = new Gio.Settings({schema_id: SOUND_SCHEMA});
        for (const key of ['sound-theme', 'disabled-sounds'])
            this._signals.push(this._settings.connect(`changed::${key}`, () => this._sync()));
        this._sync();
    }

    disable() {
        for (const id of this._signals)
            this._settings.disconnect(id);
        this._signals = [];
        // The theme stays: sounds keep their choices while the extension is off.
        this._sound = null;
    }

    _sync() {
        try {
            const dir = Gio.File.new_for_path(
                GLib.build_filenamev([GLib.get_user_data_dir(), 'sounds', USER_THEME]));
            dir.make_directory_with_parents(null);
        } catch (e) {
            if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.EXISTS)) {
                logError(e, 'Vilna: cannot make the sound theme folder');
                return;
            }
        }
        try {
            this._write();
            if (this._sound.get_string('theme-name') !== USER_THEME)
                this._sound.set_string('theme-name', USER_THEME);
        } catch (e) {
            logError(e, 'Vilna: cannot update the sound theme');
        }
    }

    _write() {
        const path = GLib.build_filenamev([GLib.get_user_data_dir(), 'sounds', USER_THEME]);
        const dir = Gio.File.new_for_path(path);
        const index = dir.get_child('index.theme');
        let existing = null;
        try {
            existing = new TextDecoder().decode(index.load_contents(null)[1]);
        } catch {
            existing = null;
        }
        const files = [];
        const children = dir.enumerate_children('standard::name', Gio.FileQueryInfoFlags.NONE, null);
        for (let info = children.next_file(null); info; info = children.next_file(null))
            files.push(info.get_name());
        children.close(null);
        const todo = plan(this._settings.get_string('sound-theme'),
            this._settings.get_strv('disabled-sounds'), existing, files);
        if (todo.index !== null) {
            // Atomic: GNOME may read the theme while we write it.
            index.replace_contents(new TextEncoder().encode(todo.index), null, false,
                Gio.FileCreateFlags.REPLACE_DESTINATION, null);
        }
        for (const name of todo.create)
            dir.get_child(name).replace_contents(new Uint8Array(0), null, false, Gio.FileCreateFlags.NONE, null);
        for (const name of todo.remove)
            dir.get_child(name).delete(null);
    }
}

// SPDX-License-Identifier: GPL-3.0-or-later
//
// (і) The glass of the dock (Dash to Dock) at the transparency the person
// chose (dock-transparency, 0–100 %), on whichever side it is. The colours
// and selectors are in lib/dock.js; this writes them as a small stylesheet
// in the runtime folder and loads it into the Shell's theme, again when
// the setting changes and when the Shell reloads its theme. The light or
// dark class on Main.uiGroup comes from colorScheme.js; this adds
// vilna-dock next to it.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {dockStylesheet} from './lib/dock.js';

const DOCK_CLASS = 'vilna-dock';

export class DockLook {
    constructor(settings) {
        this._settings = settings;
        this._file = null;
        this._serial = 0;
        this._settingsId = 0;
        this._themeId = 0;
        this._theme = null;
    }

    enable() {
        Main.uiGroup.add_style_class_name(DOCK_CLASS);
        this._settingsId = this._settings.connect('changed::dock-transparency', () => this._load());
        this._context = St.ThemeContext.get_for_stage(global.stage);
        // A new theme (a reload, or another Shell theme) drops the
        // stylesheets loaded into the old one. Loading a stylesheet emits
        // 'changed' too, with the same theme: that one is ours.
        this._themeId = this._context.connect('changed', () => {
            if (this._context.get_theme() === this._theme)
                return;
            try {
                this._file?.delete(null);
            } catch {
                // Already gone.
            }
            this._file = null;
            this._load();
        });
        this._load();
    }

    disable() {
        if (this._settingsId)
            this._settings.disconnect(this._settingsId);
        if (this._themeId)
            this._context.disconnect(this._themeId);
        this._settingsId = this._themeId = 0;
        this._unload();
        this._context = null;
        this._theme = null;
        Main.uiGroup.remove_style_class_name(DOCK_CLASS);
    }

    // For the headless test: the stylesheet in use.
    get file() {
        return this._file;
    }

    _load() {
        const theme = this._context?.get_theme();
        if (!theme)
            return;
        const dir = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'vilna-shell']);
        // A new name each time: the theme keeps a stylesheet by its file.
        const file = Gio.File.new_for_path(GLib.build_filenamev([dir, `dock-${++this._serial}.css`]));
        try {
            GLib.mkdir_with_parents(dir, 0o700);
            file.replace_contents(new TextEncoder().encode(dockStylesheet(this._settings.get_int('dock-transparency'))),
                null, false, Gio.FileCreateFlags.REPLACE_DESTINATION, null);
            this._unload();
            this._theme = theme;
            theme.load_stylesheet(file);
            this._file = file;
        } catch (e) {
            logError(e, 'Vilna: cannot load the dock stylesheet');
        }
    }

    _unload() {
        if (!this._file)
            return;
        try {
            this._context?.get_theme()?.unload_stylesheet(this._file);
        } catch (e) {
            logError(e, 'Vilna: cannot unload the dock stylesheet');
        }
        try {
            this._file.delete(null);
        } catch {
            // Already gone with the runtime folder.
        }
        this._file = null;
    }
}

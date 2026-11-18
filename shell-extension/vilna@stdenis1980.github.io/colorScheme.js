// SPDX-License-Identifier: GPL-3.0-or-later
//
// (f) Light and dark Vilna colours for what the Shell draws on the
// desktop, starting with the dock (Dash to Dock, docs/design/desktop/).
//
// GNOME Shell 46 has one stylesheet for an extension, whatever the style;
// so this puts vilna-light or vilna-dark on Main.uiGroup, the parent of
// the dock, its labels and the panel, and stylesheet.css picks colours by
// that class. It follows the style switch in Settings ▸ Appearance live.

import Gio from 'gi://Gio';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {DARK, LIGHT, schemeClass} from './lib/colorScheme.js';

export class ColorSchemeClass {
    constructor() {
        this._interface = null;
        this._changedId = 0;
    }

    enable() {
        this._interface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
        this._changedId = this._interface.connect('changed::color-scheme', () => this._sync());
        this._sync();
    }

    disable() {
        if (this._interface && this._changedId)
            this._interface.disconnect(this._changedId);
        this._changedId = 0;
        this._interface = null;
        Main.uiGroup.remove_style_class_name(LIGHT);
        Main.uiGroup.remove_style_class_name(DARK);
    }

    _sync() {
        const wanted = schemeClass(this._interface.get_string('color-scheme'));
        Main.uiGroup.remove_style_class_name(wanted === DARK ? LIGHT : DARK);
        Main.uiGroup.add_style_class_name(wanted);
    }
}

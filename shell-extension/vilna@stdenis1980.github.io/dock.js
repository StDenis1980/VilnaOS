// SPDX-License-Identifier: GPL-3.0-or-later
//
// (c) Stage B of the Gloxi window, as a scaffold: a strip at the right edge
// of the primary monitor reserved for the Gloxi panel, like a dock.
//
// The strip is a Shell actor registered as a strut, the same mechanism as
// the top bar. GNOME then keeps maximised and tiled windows out of it, and
// the notification banners, which are laid out in the work area, move left
// of it by themselves. Off by default (gloxi-dock); shown while Gloxi runs.
//
// The panel itself has to be drawn by the Shell inside this actor, as the
// design brief says for stage B: Mutter keeps every normal window at least
// 75 px inside the work area (constrain_partially_onscreen in constraints.c),
// so the GTK window of Gloxi cannot sit in a strut. Checked in GNOME Shell 46.
//
// Still to do for stage B: the panel's content, talking to Gloxi over D-Bus;
// the slide-in animation; other monitors; Gloxi's messages in the panel.

import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {dockStrip} from './lib/geometry.js';
import {findGloxiWindow} from './keepAbove.js';

export class Dock {
    constructor(settings) {
        this._settings = settings;
        this._signals = [];
        this._windowSignals = [];
        this._window = null;
        this._panel = null;
        this._idleId = 0;
    }

    enable() {
        const sync = () => this._sync();
        this._connect(this._settings, 'changed::gloxi-dock', sync);
        this._connect(this._settings, 'changed::gloxi-dock-size', sync);
        this._connect(Main.layoutManager, 'monitors-changed', sync);
        // A new window reports its app id a moment after it appears.
        this._connect(global.display, 'window-created', () => this._queueSync());
        this._sync();
    }

    disable() {
        if (this._idleId) {
            GLib.source_remove(this._idleId);
            this._idleId = 0;
        }
        for (const [object, id] of this._signals)
            object.disconnect(id);
        this._signals = [];
        this._watch(null);
        this._release();
    }

    /** The actor the stage-B panel will be drawn into, or null. */
    get panel() {
        return this._panel;
    }

    _connect(object, signal, handler) {
        this._signals.push([object, object.connect(signal, handler)]);
    }

    _queueSync() {
        if (this._idleId)
            return;
        this._idleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._idleId = 0;
            this._sync();
            return GLib.SOURCE_REMOVE;
        });
    }

    _watch(window) {
        if (window === this._window)
            return;
        for (const id of this._windowSignals)
            this._window.disconnect(id);
        this._windowSignals = [];
        this._window = window;
        if (window) {
            this._windowSignals = [
                window.connect('notify::minimized', () => this._sync()),
                window.connect('unmanaged', () => {
                    this._watch(null);
                    this._sync();
                }),
            ];
        }
    }

    _sync() {
        const window = this._settings.get_boolean('gloxi-dock') ? findGloxiWindow() : null;
        this._watch(window);
        const monitor = Main.layoutManager.primaryMonitor;
        const strip = window && !window.minimized && monitor
            ? dockStrip(monitor,
                Main.layoutManager.getWorkAreaForMonitor(Main.layoutManager.primaryIndex),
                this._settings.get_string('gloxi-dock-size'))
            : null;
        if (strip)
            this._reserve(strip);
        else
            this._release();
    }

    _reserve({x, y, width, height}) {
        if (!this._panel) {
            this._panel = new St.Widget({
                name: 'vilnaGloxiDock',
                style_class: 'vilna-gloxi-dock',
                reactive: true,
            });
            Main.layoutManager.addChrome(this._panel, {affectsStruts: true, trackFullscreen: true});
        }
        this._panel.set_position(x, y);
        this._panel.set_size(width, height);
    }

    _release() {
        if (!this._panel)
            return;
        Main.layoutManager.removeChrome(this._panel);
        this._panel.destroy();
        this._panel = null;
    }
}

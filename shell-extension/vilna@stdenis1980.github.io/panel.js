// SPDX-License-Identifier: GPL-3.0-or-later
//
// Stage A of the Gloxi window: the Shell keeps the ordinary GTK window of
// Gloxi at the right edge of the screen, like a side panel. A Wayland client
// cannot place itself, so Gloxi only says which size it wants (the
// gloxi-panel-size setting, which it changes over D-Bus) and this places it:
// when the window first shows, and again when the size or the work area
// changes. The person may still move it away by hand; it comes back to the
// edge only on the next of those events.
//
// While the stage-B strip is on (gloxi-dock) this does nothing: the strip
// owns the edge.

import GLib from 'gi://GLib';
import Meta from 'gi://Meta';

import {panelRect} from './lib/geometry.js';
import {findGloxiWindow, roleOf} from './keepAbove.js';

function isMaximized(window) {
    // GNOME 49+ has is_maximized(); before that, the maximized-* flags.
    if (typeof window.is_maximized === 'function')
        return window.is_maximized();
    return Boolean(window.maximized_horizontally || window.maximized_vertically);
}

export class PanelPlacement {
    constructor(settings) {
        this._settings = settings;
        this._signals = [];
        this._idleId = 0;
        // New windows not placed yet: window → [[object, signal id]].
        this._pending = new Map();
    }

    enable() {
        const replace = () => this._queuePlace();
        for (const key of ['gloxi-panel-edge', 'gloxi-panel-size', 'gloxi-dock'])
            this._connect(this._settings, `changed::${key}`, replace);
        this._connect(global.display, 'workareas-changed', replace);
        this._connect(global.display, 'window-created', (_d, window) => this._waitForFrame(window));
        this._queuePlace();
    }

    disable() {
        if (this._idleId) {
            GLib.source_remove(this._idleId);
            this._idleId = 0;
        }
        for (const [object, id] of this._signals)
            object.disconnect(id);
        this._signals = [];
        for (const window of [...this._pending.keys()])
            this._forget(window);
    }

    _connect(object, signal, handler) {
        this._signals.push([object, object.connect(signal, handler)]);
    }

    // A new Wayland window is announced with its first buffer, before it
    // reports its app id and before Mutter places it: wait for both the id
    // and the window being shown, then place it once (placing it earlier
    // is undone by Mutter's own placement). Other windows are let go as
    // soon as their id shows they are not Gloxi.
    _waitForFrame(window) {
        if (!window || this._pending.has(window))
            return;
        let shown = false;
        const check = () => {
            const role = roleOf(window);
            if (role === 'gloxi' && shown)
                this._queuePlace();
            else if (role && role !== 'gloxi')
                this._forget(window);
            else if (!role && window.get_wm_class())
                this._forget(window);
        };
        const ids = [
            [window, window.connect('notify::wm-class', check)],
            [window, window.connect('notify::gtk-application-id', check)],
            [window, window.connect('shown', () => {
                shown = true;
                check();
            })],
            [window, window.connect('unmanaged', () => this._forget(window))],
        ];
        this._pending.set(window, ids);
        check();
    }

    _forget(window) {
        for (const [object, id] of this._pending.get(window) ?? [])
            object.disconnect(id);
        this._pending.delete(window);
    }

    _queuePlace() {
        if (this._idleId)
            return;
        this._idleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._idleId = 0;
            this._place();
            return GLib.SOURCE_REMOVE;
        });
    }

    _place() {
        if (!this._settings.get_boolean('gloxi-panel-edge') || this._settings.get_boolean('gloxi-dock'))
            return;
        const window = findGloxiWindow();
        if (!window || window.minimized || window.is_fullscreen() || isMaximized(window))
            return;
        if (window.get_window_type() !== Meta.WindowType.NORMAL)
            return;
        // Placed: a new window is placed once, later only on changes.
        this._forget(window);
        const rect = panelRect(window.get_work_area_current_monitor(),
            this._settings.get_string('gloxi-panel-size'), window.get_frame_rect());
        window.move_resize_frame(false, rect.x, rect.y, rect.width, rect.height);
    }
}

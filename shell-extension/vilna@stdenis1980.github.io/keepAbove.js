// SPDX-License-Identifier: GPL-3.0-or-later
//
// (b) Gloxi above all windows on Wayland.
//
// A Wayland client cannot ask to stay on top, so the Shell does it: when a
// Gloxi window appears, make it "above" (the same flag as "Always on Top" in
// the window menu). The setting is read when a window appears and when it
// changes; if the person turns "Always on Top" off by hand, that is left as
// it is. The Herkos confirmation dialog is always kept above, so Gloxi on
// top never hides the question.

import {wantsAbove, windowRole} from './lib/windowRules.js';

export function roleOf(window) {
    return windowRole([window.get_wm_class(), window.get_gtk_application_id()]);
}

export class KeepAbove {
    constructor(settings) {
        this._settings = settings;
        this._signals = [];
        // Windows we made "above", so we only ever undo our own change.
        this._raised = new Set();
        // Per-window handlers, for ids that arrive after the window does.
        this._windowSignals = new Map();
    }

    enable() {
        this._connect(global.display, 'window-created', (_d, window) => this._track(window));
        this._connect(this._settings, 'changed::gloxi-keep-above', () => this._applyAll());
        for (const actor of global.get_window_actors())
            this._track(actor.meta_window);
    }

    disable() {
        for (const [object, id] of this._signals)
            object.disconnect(id);
        this._signals = [];
        for (const [window, ids] of this._windowSignals) {
            for (const id of ids)
                window.disconnect(id);
        }
        this._windowSignals.clear();
        for (const window of this._raised) {
            if (window.is_above())
                window.unmake_above();
        }
        this._raised.clear();
    }

    _connect(object, signal, handler) {
        this._signals.push([object, object.connect(signal, handler)]);
    }

    _track(window) {
        if (!window || this._windowSignals.has(window))
            return;
        const recheck = () => this._apply(window);
        this._windowSignals.set(window, [
            window.connect('notify::wm-class', recheck),
            window.connect('notify::gtk-application-id', recheck),
            window.connect('unmanaged', () => this._forget(window)),
        ]);
        this._apply(window);
    }

    _forget(window) {
        for (const id of this._windowSignals.get(window) ?? [])
            window.disconnect(id);
        this._windowSignals.delete(window);
        this._raised.delete(window);
    }

    _applyAll() {
        for (const window of this._windowSignals.keys())
            this._apply(window, true);
    }

    _apply(window, settingChanged = false) {
        const role = roleOf(window);
        if (!role)
            return;
        const want = wantsAbove(role, {
            gloxiKeepAbove: this._settings.get_boolean('gloxi-keep-above'),
            transient: window.get_transient_for() !== null,
        });
        if (want && !window.is_above()) {
            // A window seen before keeps the person's own choice, unless the
            // setting itself has just changed.
            if (this._raised.has(window) && !settingChanged)
                return;
            window.make_above();
            this._raised.add(window);
        } else if (!want && this._raised.has(window)) {
            if (window.is_above())
                window.unmake_above();
            this._raised.delete(window);
        }
    }
}

// Used by the D-Bus service and the dock to find the Gloxi window.
export function findGloxiWindow() {
    return global.display.list_all_windows()
        .find(window => window && roleOf(window) === 'gloxi' &&
            window.get_transient_for() === null) ?? null;
}

// SPDX-License-Identifier: GPL-3.0-or-later
//
// (a) A Latin layout in password fields, as on macOS.
//
// GNOME already tells the compositor what kind of text a field takes: GTK's
// password entries (through the Wayland text-input protocol) and the Shell's
// own St.PasswordEntry (lock screen, login screen, Wi-Fi, encrypted disk and
// polkit dialogs) set the input purpose to PASSWORD. The Shell's input method
// sees it as its `content-purpose` property. So one mechanism covers every
// field, and no key presses are read: only the purpose and focus.
//
// Focus out has no signal of its own; the Shell's input method hides the
// on-screen keyboard 50 ms after it, through 'input-panel-state', and a new
// field in between changes the purpose. Each of these schedules a check of
// "focus is in a password field".

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Keyboard from 'resource:///org/gnome/shell/ui/status/keyboard.js';

import {nextSource} from './lib/inputSources.js';
import {PasswordSession} from './lib/passwordSession.js';
import {indicatorPosition, monitorAt} from './lib/geometry.js';

function sourcesOf(manager) {
    return Object.values(manager?.inputSources ?? {})
        .filter(Boolean)
        .sort((a, b) => a.index - b.index);
}

export class PasswordLayout {
    constructor(settings) {
        this._settings = settings;
        this._session = new PasswordSession();
        this._signals = [];
        this._syncId = 0;
        this._cursor = null;
        this._noLatin = false;
        this._reapply = false;
        this._indicator = null;
    }

    enable() {
        this._im = Main.inputMethod;
        this._manager = Keyboard.getInputSourceManager();
        if (!this._im || !this._manager)
            return;
        const queue = () => this._queueSync();
        this._connect(this._im, 'notify::content-purpose', queue);
        this._connect(this._im, 'input-panel-state', queue);
        this._connect(this._im, 'cursor-location-changed', (_im, rect) => {
            this._cursor = rect
                ? {x: rect.get_x(), y: rect.get_y(), width: rect.get_width(), height: rect.get_height()}
                : null;
            this._queueSync();
        });
        this._connect(this._manager, 'current-source-changed', () => this._updateIndicator());
        // After 'sources-changed' GNOME activates the usual layout again;
        // check once that has happened.
        this._connect(this._manager, 'sources-changed', () => {
            this._reapply = true;
            this._queueSync();
        });
        this._connect(this._settings, 'changed::password-latin-layout', queue);
        this._queueSync();
    }

    disable() {
        if (this._syncId) {
            GLib.source_remove(this._syncId);
            this._syncId = 0;
        }
        for (const [object, id] of this._signals)
            object.disconnect(id);
        this._signals = [];
        // Give the person's layout back if the field still has focus.
        if (this._session.active)
            this._leave();
        this._indicator?.destroy();
        this._indicator = null;
        this._im = null;
        this._manager = null;
    }

    _connect(object, signal, handler) {
        this._signals.push([object, object.connect(signal, handler)]);
    }

    _queueSync() {
        if (this._syncId)
            return;
        this._syncId = GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
            this._syncId = 0;
            this._sync();
            return GLib.SOURCE_REMOVE;
        });
    }

    _inPasswordField() {
        return this._settings.get_boolean('password-latin-layout') &&
            this._im?.currentFocus != null &&
            this._im.content_purpose === Clutter.InputContentPurpose.PASSWORD;
    }

    _sync() {
        const want = this._inPasswordField();
        if (want && !this._session.active)
            this._enter();
        else if (!want && this._session.active)
            this._leave();
        else if (want && this._reapply)
            this._switchIfNeeded(this._session.reapply(
                sourcesOf(this._manager), this._manager.currentSource, this._preferred()));
        this._reapply = false;
        this._updateIndicator();
    }

    _preferred() {
        return this._settings.get_strv('password-preferred-layouts');
    }

    _enter() {
        this._switchIfNeeded(this._session.enter(
            sourcesOf(this._manager), this._manager.currentSource, this._preferred()));
    }

    _switchIfNeeded(decision) {
        this._noLatin = decision.action === 'no-latin';
        // Not interactive: the person's most-recent order is left alone.
        if (decision.action === 'switch')
            decision.to.activate(false);
    }

    _leave() {
        const decision = this._session.leave(
            sourcesOf(this._manager), this._manager?.currentSource);
        this._noLatin = false;
        this._cursor = null;
        // Interactive, so the saved current layout is the one given back
        // even if the person switched by hand inside the field.
        if (decision.action === 'switch')
            decision.to.activate(true);
    }

    _ensureIndicator() {
        if (this._indicator)
            return this._indicator;
        this._indicator = new St.Button({
            style_class: 'vilna-layout-indicator',
            can_focus: false,
            reactive: true,
            track_hover: true,
            visible: false,
        });
        // Switching by hand: the next configured layout. Inside modal Shell
        // dialogs (lock and login screens) clicks do not reach it; there the
        // usual Super+Space and the top bar keyboard menu still work.
        this._indicator.connect('clicked', () => {
            const next = nextSource(sourcesOf(this._manager), this._manager?.currentSource);
            next?.activate(false);
        });
        Main.layoutManager.addTopChrome(this._indicator);
        return this._indicator;
    }

    _updateIndicator() {
        if (!this._session.active) {
            if (this._indicator)
                this._indicator.visible = false;
            return;
        }
        const indicator = this._ensureIndicator();
        const current = this._manager?.currentSource;
        indicator.label = current?.shortName ?? '?';
        indicator.accessible_name = current?.displayName ?? '';
        if (this._noLatin)
            indicator.add_style_class_name('vilna-layout-indicator-warning');
        else
            indicator.remove_style_class_name('vilna-layout-indicator-warning');

        // Shell dialogs may not report a cursor; then the indicator sits at
        // the top centre of the primary monitor.
        const primary = Main.layoutManager.primaryMonitor;
        const [, natWidth] = indicator.get_preferred_width(-1);
        const [, natHeight] = indicator.get_preferred_height(natWidth);
        const cursor = this._cursor ?? (primary
            ? {x: primary.x + (primary.width - natWidth) / 2, y: primary.y + primary.height / 4, width: 0, height: 0}
            : {x: 0, y: 0, width: 0, height: 0});
        const monitor = monitorAt(Main.layoutManager.monitors, cursor.x, cursor.y);
        const {x, y} = indicatorPosition(cursor, {width: natWidth, height: natHeight}, monitor);
        indicator.set_position(x, y);
        indicator.visible = true;
    }
}

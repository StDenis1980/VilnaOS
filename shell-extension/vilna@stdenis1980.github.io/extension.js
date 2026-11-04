// SPDX-License-Identifier: GPL-3.0-or-later
//
// Vilna's GNOME Shell extension. See ../README.md.
//
// It runs in three session modes:
//  - user: everything (password layout, Gloxi above, Gloxi strip, D-Bus);
//  - unlock-dialog: kept enabled while the screen is locked, so the
//    password field of the lock screen gets the Latin layout too;
//  - gdm: the login screen, where only the password layout runs.

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {Dock} from './dock.js';
import {KeepAbove} from './keepAbove.js';
import {PasswordLayout} from './passwordLayout.js';
import {VilnaShellService} from './dbus.js';

export default class VilnaExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._password = new PasswordLayout(this._settings);
        this._password.enable();
        this._userParts = null;
        this._sessionId = Main.sessionMode.connect('updated', () => this._syncSessionMode());
        this._syncSessionMode();
    }

    disable() {
        // Called on logout and when the extension is turned off; while the
        // screen is locked it stays enabled (unlock-dialog).
        Main.sessionMode.disconnect(this._sessionId);
        this._sessionId = 0;
        this._stopUserParts();
        this._password.disable();
        this._password = null;
        this._settings = null;
    }

    _syncSessionMode() {
        // Windows exist only in the person's own session, not on the login
        // screen. While locked the parts keep running: the windows are
        // hidden anyway, and stopping would reshuffle them on every unlock.
        const greeter = Main.sessionMode.isGreeter;
        if (!greeter && !this._userParts)
            this._startUserParts();
        else if (greeter && this._userParts)
            this._stopUserParts();
    }

    _startUserParts() {
        this._userParts = [
            new KeepAbove(this._settings),
            new Dock(this._settings),
            new VilnaShellService(this._settings),
        ];
        for (const part of this._userParts)
            part.enable();
    }

    _stopUserParts() {
        for (const part of (this._userParts ?? []).reverse())
            part.disable();
        this._userParts = null;
    }
}

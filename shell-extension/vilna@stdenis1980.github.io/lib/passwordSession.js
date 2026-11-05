// SPDX-License-Identifier: GPL-3.0-or-later
//
// The state of one visit to a password field: remember the source the
// person had, switch to a Latin one, and give the old one back on leaving.
// Pure logic, tested under node (tests/passwordSession.test.js).

import {decideOnEnter, sourceKey} from './inputSources.js';

export class PasswordSession {
    constructor() {
        this._active = false;
        this._previous = null;
    }

    /** True while a password field has focus. */
    get active() {
        return this._active;
    }

    /**
     * A password field got focus. Returns what decideOnEnter() returns;
     * a second call while already inside returns {action: 'stay'}, so moving
     * from "password" to "repeat password" keeps the original to restore.
     */
    enter(sources, current, preferred = []) {
        if (this._active)
            return {action: 'stay'};
        this._active = true;
        this._previous = current ? {type: current.type, id: current.id} : null;
        return decideOnEnter(sources, current, preferred);
    }

    /**
     * The list of layouts was reloaded while inside the field (changed in
     * Settings): GNOME then activates the person's usual layout again, so
     * decide once more, keeping the layout to give back on leaving.
     */
    reapply(sources, current, preferred = []) {
        if (!this._active)
            return {action: 'stay'};
        return decideOnEnter(sources, current, preferred);
    }

    /**
     * The password field lost focus. Returns {action: 'switch', to} with the
     * source the person had before, or {action: 'stay'} when it is already
     * current or no longer configured (removed in Settings meanwhile).
     */
    leave(sources, current) {
        if (!this._active)
            return {action: 'stay'};
        this._active = false;
        const previous = this._previous;
        this._previous = null;
        if (!previous || sourceKey(previous) === sourceKey(current))
            return {action: 'stay'};
        const list = Array.isArray(sources) ? sources : [];
        const to = list.find(s => s && sourceKey(s) === sourceKey(previous));
        return to ? {action: 'switch', to} : {action: 'stay'};
    }
}

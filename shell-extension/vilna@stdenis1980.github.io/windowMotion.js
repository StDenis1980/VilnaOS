// SPDX-License-Identifier: GPL-3.0-or-later
//
// (ж) Vilna's window motion (docs/design/desktop/README.md): how windows
// open, close, go to the dock and come back, and lift while dragged.
// The numbers and the choice per style are in lib/windowMotion.js.
//
// GNOME Shell keeps doing the bookkeeping of its own window animations
// (windowManager.js: which windows are animating, completed_map() and the
// rest), so nothing can get stuck half-shown. Only the look changes: when
// Shell decides to animate a normal window (_shouldAnimateActor), this
// puts a one-time ease() on that window that swaps GNOME's parameters for
// Vilna's before handing them on. Dialogs, maximising and tiling keep
// GNOME's motion. With animations off in Settings, GNOME animates
// nothing and neither does this.

import Clutter from 'gi://Clutter';
import Meta from 'gi://Meta';
import {InjectionManager} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {liftFor, liftPivot, motionFor} from './lib/windowMotion.js';

// GNOME Shell's sets of windows it is animating, by event.
const EVENTS = [
    ['_mapping', 'map'],
    ['_destroying', 'destroy'],
    ['_minimizing', 'minimize'],
    ['_unminimizing', 'unminimize'],
];

const MOVING = new Set([Meta.GrabOp.MOVING, Meta.GrabOp.KEYBOARD_MOVING]
    .filter(op => op !== undefined));
// A drag past the screen edge (Shift) adds this flag to the operation.
const UNCONSTRAINED = Meta.GrabOp.WINDOW_FLAG_UNCONSTRAINED ?? 0;

function isNormal(actor) {
    const window = actor.meta_window;
    return Boolean(window) &&
        window.get_window_type() === Meta.WindowType.NORMAL &&
        !window.get_transient_for();
}

function eventOf(actor) {
    for (const [set, event] of EVENTS) {
        if (Main.wm[set]?.has?.(actor))
            return event;
    }
    return null;
}

function apply(actor, start) {
    if (!start)
        return;
    const {pivot, ...props} = start;
    if (pivot)
        actor.set_pivot_point(...pivot);
    for (const [key, value] of Object.entries(props))
        actor[key] = value;
}

function clutterParams(params) {
    const mode = Clutter.AnimationMode[params.mode] ?? Clutter.AnimationMode.EASE_OUT_QUAD;
    return {...params, mode};
}

export class WindowMotion {
    constructor(settings) {
        this._settings = settings;
        this._injections = null;
        this._grabSignals = [];
        this._lifted = null;
    }

    get style() {
        return this._settings.get_string('window-motion');
    }

    enable() {
        if (typeof Main.wm?._shouldAnimateActor !== 'function') {
            console.warn('Vilna: this GNOME Shell has no _shouldAnimateActor; window motion stays GNOME\'s');
            return;
        }
        this._injections = new InjectionManager();
        const self = this;
        this._injections.overrideMethod(Object.getPrototypeOf(Main.wm), '_shouldAnimateActor', original => function (actor, types) {
            const animate = original.call(this, actor, types);
            if (animate && isNormal(actor))
                self._takeOver(actor);
            return animate;
        });
        this._grabSignals = [
            global.display.connect('grab-op-begin', (_d, window, op) => this._lift(window, op, 'begin')),
            global.display.connect('grab-op-end', (_d, window, op) => this._lift(window, op, 'end')),
        ];
    }

    disable() {
        this._injections?.clear();
        this._injections = null;
        for (const id of this._grabSignals)
            global.display.disconnect(id);
        this._grabSignals = [];
        if (this._lifted)
            this._lift(this._lifted.meta_window, Meta.GrabOp.MOVING, 'end');
        this._lifted = null;
    }

    // The next ease() GNOME starts on this window gets Vilna's motion.
    _takeOver(actor) {
        const style = this.style;
        if (style === 'gnome')
            return;
        actor.ease = function (params) {
            delete this.ease;
            const event = eventOf(this);
            const motion = event ? motionFor(event, style, params) : null;
            if (!motion)
                return Clutter.Actor.prototype.ease.call(this, params);
            apply(this, motion.start);
            return Clutter.Actor.prototype.ease.call(this, clutterParams(motion.params));
        };
    }

    _lift(window, op, phase) {
        if (!window || !MOVING.has(op & ~UNCONSTRAINED) || window.get_window_type() !== Meta.WindowType.NORMAL)
            return;
        const actor = window.get_compositor_private();
        if (!actor)
            return;
        const lift = liftFor(phase, this.style);
        if (!lift) {
            if (phase === 'end' && this._lifted === actor)
                this._settle(actor);
            return;
        }
        if (phase === 'begin') {
            const [px, py] = global.get_pointer();
            actor.set_pivot_point(...liftPivot([px, py], {x: actor.x, y: actor.y, width: actor.width, height: actor.height}));
            this._lifted = actor;
            actor.ease(clutterParams(lift));
        } else {
            this._lifted = null;
            actor.ease({...clutterParams(lift), onStopped: () => actor.set_pivot_point(0, 0)});
        }
    }

    _settle(actor) {
        this._lifted = null;
        actor.remove_all_transitions();
        actor.set_scale(1, 1);
        actor.set_pivot_point(0, 0);
    }
}

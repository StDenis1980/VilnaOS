// SPDX-License-Identifier: GPL-3.0-or-later
//
// (ж) "Rubber" windows: while a window is dragged it bends a little, the
// spot under the pointer leading and the far side lagging, and when it
// is let go it springs back to its shape. Our own spring grid
// (lib/wobble.js) drives a Clutter.DeformEffect, which redraws the
// window's picture on a mesh; the window itself, its size and its input
// stay as they are. Only for normal windows, only in Vilna's motion
// style with window-wobble on, and never with animations off.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import St from 'gi://St';

import {Jelly, wobbles} from './lib/wobble.js';
import {liftPivot} from './lib/windowMotion.js';

const MOVING = new Set([Meta.GrabOp.MOVING, Meta.GrabOp.KEYBOARD_MOVING]
    .filter(op => op !== undefined));
const UNCONSTRAINED = Meta.GrabOp.WINDOW_FLAG_UNCONSTRAINED ?? 0;
const EFFECT = 'vilna-wobble';
// Mesh tiles each way: enough for a smooth bend, cheap to draw.
const TILES = 10;

const WobbleEffect = GObject.registerClass(
class VilnaWobbleEffect extends Clutter.DeformEffect {
    _init(jelly) {
        super._init();
        this.jelly = jelly;
        this.set_n_tiles(TILES, TILES);
    }

    vfunc_deform_vertex(_width, _height, vertex) {
        const [dx, dy] = this.jelly.at(vertex.tx, vertex.ty);
        vertex.x += dx;
        vertex.y += dy;
    }
});

export class Wobble {
    constructor(settings) {
        this._settings = settings;
        this._signals = [];
        this._active = new Map(); // actor -> {window, jelly, effect, timeline, ...}
    }

    enable() {
        this._signals = [
            global.display.connect('grab-op-begin', (_d, window, op) => this._begin(window, op)),
            global.display.connect('grab-op-end', (_d, window, op) => this._end(window, op)),
        ];
    }

    disable() {
        for (const id of this._signals)
            global.display.disconnect(id);
        this._signals = [];
        for (const actor of [...this._active.keys()])
            this._stop(actor);
    }

    // For the headless test: the wobble on a window, if any.
    stateOf(window) {
        const state = this._active.get(window?.get_compositor_private?.());
        return state ? {dragging: state.dragging, jelly: state.jelly} : null;
    }

    _on() {
        return wobbles(this._settings.get_string('window-motion'), this._settings.get_boolean('window-wobble')) &&
            St.Settings.get().enable_animations;
    }

    _begin(window, op) {
        if (!window || !MOVING.has(op & ~UNCONSTRAINED) || window.get_window_type() !== Meta.WindowType.NORMAL || !this._on())
            return;
        const actor = window.get_compositor_private();
        if (!actor)
            return;
        let state = this._active.get(actor);
        if (!state) {
            const jelly = new Jelly();
            const effect = new WobbleEffect(jelly);
            actor.add_effect_with_name(EFFECT, effect);
            const timeline = new Clutter.Timeline({actor, duration: 1000, repeat_count: -1});
            state = {window, jelly, effect, timeline, dragging: true, last: GLib.get_monotonic_time()};
            state.frameId = timeline.connect('new-frame', () => this._frame(actor));
            state.destroyId = actor.connect('destroy', () => this._stop(actor));
            this._active.set(actor, state);
            timeline.start();
        }
        const [px, py] = global.get_pointer();
        state.jelly.grab(...liftPivot([px, py], {x: actor.x, y: actor.y, width: actor.width, height: actor.height}));
        const rect = window.get_frame_rect();
        state.at = [rect.x, rect.y];
        state.dragging = true;
        state.moveId ??= window.connect('position-changed', () => this._moved(actor));
    }

    _end(window, op) {
        if (!window || !MOVING.has(op & ~UNCONSTRAINED))
            return;
        const state = this._active.get(window.get_compositor_private());
        if (!state)
            return;
        state.dragging = false;
        if (state.moveId) {
            window.disconnect(state.moveId);
            state.moveId = 0;
        }
    }

    _moved(actor) {
        const state = this._active.get(actor);
        if (!state)
            return;
        const rect = state.window.get_frame_rect();
        state.jelly.move(rect.x - state.at[0], rect.y - state.at[1]);
        state.at = [rect.x, rect.y];
    }

    _frame(actor) {
        const state = this._active.get(actor);
        if (!state)
            return;
        const now = GLib.get_monotonic_time();
        state.jelly.step((now - state.last) / 1e6);
        state.last = now;
        if (!state.dragging && state.jelly.settled()) {
            this._stop(actor);
            return;
        }
        state.effect.invalidate();
    }

    _stop(actor) {
        const state = this._active.get(actor);
        if (!state)
            return;
        this._active.delete(actor);
        state.timeline.disconnect(state.frameId);
        state.timeline.stop();
        if (state.moveId)
            state.window.disconnect(state.moveId);
        actor.disconnect(state.destroyId);
        actor.remove_effect_by_name(EFFECT);
    }
}

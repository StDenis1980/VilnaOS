// SPDX-License-Identifier: GPL-3.0-or-later
//
// The live sky behind the weather card: draws lib/weatherSky.js with
// Cairo in an St.DrawingArea. It moves only when it can be seen and
// the person wants motion: it stops under a window, in the overview,
// on the lock screen and when animations are off (then it is one still
// frame), so it costs nothing while someone works.

import Cairo from 'cairo';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {FPS, newSky, skyShapes, stepSky} from './lib/weatherSky.js';

// How often to look whether a window covers the card, in seconds.
const COVER_CHECK = 1;

function roundedRect(cr, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    cr.newSubPath();
    cr.arc(w - r, r, r, -Math.PI / 2, 0);
    cr.arc(w - r, h - r, r, 0, Math.PI / 2);
    cr.arc(r, h - r, r, Math.PI / 2, Math.PI);
    cr.arc(r, r, r, Math.PI, 1.5 * Math.PI);
    cr.closePath();
}

// Cairo for one frame of shapes (lib/weatherSky.js skyShapes).
export function drawShapes(cr, shapes, w, h) {
    for (const s of shapes) {
        switch (s.op) {
        case 'sky': {
            roundedRect(cr, w, h, s.radius);
            cr.clip();
            const g = new Cairo.LinearGradient(0, 0, 0, h);
            g.addColorStopRGBA(0, ...s.top);
            g.addColorStopRGBA(1, ...s.bottom);
            cr.setSource(g);
            cr.paint();
            break;
        }
        case 'glow': {
            const g = new Cairo.RadialGradient(s.x, s.y, 0, s.x, s.y, s.r);
            g.addColorStopRGBA(0, ...s.colour);
            g.addColorStopRGBA(1, s.colour[0], s.colour[1], s.colour[2], 0);
            cr.setSource(g);
            cr.arc(s.x, s.y, s.r, 0, 2 * Math.PI);
            cr.fill();
            break;
        }
        case 'circle':
            cr.setSourceRGBA(...s.colour);
            cr.arc(s.x, s.y, s.r, 0, 2 * Math.PI);
            cr.fill();
            break;
        case 'cloud':
            cr.setSourceRGBA(...s.colour);
            cr.newPath();
            for (const [x, y, r] of s.circles) {
                cr.newSubPath();
                cr.arc(x, y, r, 0, 2 * Math.PI);
            }
            cr.fill(); // the default winding fills the union once
            break;
        case 'line':
            if (s.points.length < 2)
                break;
            cr.setSourceRGBA(...s.colour);
            cr.setLineWidth(s.width);
            cr.setLineCap(Cairo.LineCap.ROUND);
            cr.moveTo(...s.points[0]);
            for (const p of s.points.slice(1))
                cr.lineTo(...p);
            cr.stroke();
            break;
        case 'flash':
            cr.setSourceRGBA(...s.colour);
            cr.paint();
            break;
        }
    }
}

export class Sky {
    constructor() {
        this._sky = null;
        this._kind = null;
        this._day = true;
        this._frame = 0;
        this._coverTimer = 0;
        this._last = 0;
        this.actor = new St.DrawingArea({x_expand: true, y_expand: true, reactive: false});
        this.actor.name = 'vilnaWeatherSky';
        this.actor.connect('repaint', area => this._repaint(area));
        for (const property of ['notify::width', 'notify::height'])
            this.actor.connect(property, () => this._resize());
        this.actor.connect('destroy', () => this._stopAll());
        this._settings = St.Settings.get();
        this._animId = this._settings.connect('notify::enable-animations', () => this._update());
        this._lockId = Main.sessionMode.connect('updated', () => this._update());
    }

    destroy() {
        this._stopAll();
        this._settings.disconnect(this._animId);
        Main.sessionMode.disconnect(this._lockId);
        this.actor.destroy();
    }

    // For the headless test.
    get kind() {
        return this._kind;
    }

    get moving() {
        return this._frame !== 0;
    }

    show(kind, day) {
        if (kind === this._kind && day === this._day && this._sky)
            return;
        this._kind = kind;
        this._day = day;
        this._sky = null;
        this._resize();
        // Clutter tells nothing when an ancestor hides or shows, nor when
        // a window comes over: look again every second (a few rectangle
        // comparisons) while the sky exists.
        if (!this._coverTimer) {
            this._coverTimer = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, COVER_CHECK, () => {
                this._update();
                return GLib.SOURCE_CONTINUE;
            });
        }
        this._update();
    }

    _resize() {
        // get_surface_size() is for the repaint handler only.
        const box = this.actor.get_allocation_box();
        const [w, h] = [Math.round(box.get_width()), Math.round(box.get_height())];
        if (!this._kind || w < 2 || h < 2)
            return;
        if (!this._sky || this._sky.w !== w || this._sky.h !== h) {
            this._sky = newSky(this._kind, this._day, w, h);
            stepSky(this._sky, 0.25);
            this.actor.queue_repaint();
        }
    }

    _repaint(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        try {
            if (this._sky)
                drawShapes(cr, skyShapes(this._sky), w, h);
        } catch (e) {
            console.warn(`Vilna widgets: cannot draw the sky: ${e.message}`);
        } finally {
            cr.$dispose();
        }
    }

    _canMove() {
        return Boolean(this._sky) && this.actor.mapped && this._settings.enable_animations &&
            !Main.sessionMode.isLocked && !Main.overview.visible && !this._covered();
    }

    // A window of the active workspace over the card: then the sky stops.
    _covered() {
        const [x, y] = this.actor.get_transformed_position();
        const [w, h] = this.actor.get_transformed_size();
        const workspace = global.workspace_manager.get_active_workspace();
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (!win || win.minimized || !win.showing_on_its_workspace() || !win.located_on_workspace(workspace))
                continue;
            const r = win.get_frame_rect();
            if (r.x < x + w && r.x + r.width > x && r.y < y + h && r.y + r.height > y)
                return true;
        }
        return false;
    }

    _update() {
        this._resize();
        if (this._canMove())
            this._start();
        else
            this._stop();
    }

    _start() {
        if (this._frame)
            return;
        this._last = GLib.get_monotonic_time();
        this._frame = GLib.timeout_add(GLib.PRIORITY_DEFAULT_IDLE, Math.round(1000 / FPS), () => {
            const now = GLib.get_monotonic_time();
            stepSky(this._sky, (now - this._last) / 1e6);
            this._last = now;
            this.actor.queue_repaint();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stop() {
        if (this._frame)
            GLib.source_remove(this._frame);
        this._frame = 0;
    }

    _stopAll() {
        this._stop();
        if (this._coverTimer)
            GLib.source_remove(this._coverTimer);
        this._coverTimer = 0;
    }
}

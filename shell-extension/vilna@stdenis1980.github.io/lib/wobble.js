// SPDX-License-Identifier: GPL-3.0-or-later
//
// (ж) The "rubber" window while it is dragged: a small grid of springs
// over the window. The spot under the pointer follows the pointer at
// once; the farther a part of the window is from it, the more it lags
// behind, then springs back with one soft overshoot. Pure logic, no
// GNOME: windowMotion's wobble effect asks it where each corner of its
// mesh goes, and tests/wobble.test.js checks it.

export const COLS = 4;
export const ROWS = 4;
// Spring per second²; damping ratio below 1 gives one small overshoot.
export const STIFFNESS = 260;
export const DAMPING_RATIO = 0.42;
// How far from the grab point the pull fades (share of the window side).
export const REACH = 0.42;
// No part of the window strays further than this, in pixels.
export const MAX_OFFSET = 40;
// A long frame (the Shell was busy) is cut to this many seconds.
export const MAX_STEP = 1 / 30;
const REST = 0.4; // px and px/s under which the window counts as still

const finite = v => Number.isFinite(v) ? v : 0;
const clamp01 = v => Math.min(1, Math.max(0, finite(v)));

export class Jelly {
    constructor(cols = COLS, rows = ROWS) {
        this.cols = Math.max(2, Math.floor(cols));
        this.rows = Math.max(2, Math.floor(rows));
        const n = this.cols * this.rows;
        this.ox = new Float64Array(n);
        this.oy = new Float64Array(n);
        this.vx = new Float64Array(n);
        this.vy = new Float64Array(n);
        this.pull = new Float64Array(n).fill(1);
    }

    // The point being dragged, as a share of the window (0..1 each way).
    grab(u, v) {
        u = clamp01(u);
        v = clamp01(v);
        for (let j = 0; j < this.rows; j++) {
            for (let i = 0; i < this.cols; i++) {
                const du = i / (this.cols - 1) - u;
                const dv = j / (this.rows - 1) - v;
                this.pull[j * this.cols + i] = Math.exp(-(du * du + dv * dv) / (2 * REACH * REACH));
            }
        }
    }

    // The window moved by (dx, dy) px: parts far from the grab point
    // stay behind for a moment.
    move(dx, dy) {
        dx = finite(dx);
        dy = finite(dy);
        for (let k = 0; k < this.ox.length; k++) {
            const lag = 1 - this.pull[k];
            this.ox[k] = clampOffset(this.ox[k] - dx * lag);
            this.oy[k] = clampOffset(this.oy[k] - dy * lag);
        }
    }

    // Let the springs pull every part back for dt seconds.
    step(dt) {
        dt = Math.min(MAX_STEP, Math.max(0, finite(dt)));
        if (dt === 0)
            return;
        const c = 2 * DAMPING_RATIO * Math.sqrt(STIFFNESS);
        for (let k = 0; k < this.ox.length; k++) {
            this.vx[k] += (-STIFFNESS * this.ox[k] - c * this.vx[k]) * dt;
            this.vy[k] += (-STIFFNESS * this.oy[k] - c * this.vy[k]) * dt;
            this.ox[k] = clampOffset(this.ox[k] + this.vx[k] * dt);
            this.oy[k] = clampOffset(this.oy[k] + this.vy[k] * dt);
        }
    }

    // Offset in px at a point of the window (0..1 each way), between the
    // four nearest springs.
    at(u, v) {
        const x = clamp01(u) * (this.cols - 1);
        const y = clamp01(v) * (this.rows - 1);
        const i = Math.min(this.cols - 2, Math.floor(x));
        const j = Math.min(this.rows - 2, Math.floor(y));
        const fx = x - i, fy = y - j;
        const k = j * this.cols + i;
        const mix = a => (a[k] * (1 - fx) + a[k + 1] * fx) * (1 - fy) +
            (a[k + this.cols] * (1 - fx) + a[k + this.cols + 1] * fx) * fy;
        return [mix(this.ox), mix(this.oy)];
    }

    settled() {
        for (let k = 0; k < this.ox.length; k++) {
            if (Math.abs(this.ox[k]) > REST || Math.abs(this.oy[k]) > REST ||
                Math.abs(this.vx[k]) > REST || Math.abs(this.vy[k]) > REST)
                return false;
        }
        return true;
    }

    reset() {
        for (const a of [this.ox, this.oy, this.vx, this.vy])
            a.fill(0);
    }
}

function clampOffset(v) {
    return Math.min(MAX_OFFSET, Math.max(-MAX_OFFSET, finite(v)));
}

// Whether a drag wobbles: only in Vilna's motion style, with the setting on.
export function wobbles(style, enabled) {
    return style === 'vilna' && enabled === true;
}

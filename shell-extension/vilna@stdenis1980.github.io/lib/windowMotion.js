// SPDX-License-Identifier: GPL-3.0-or-later
//
// How Vilna moves windows: opening, closing, minimising to the dock and
// back, and lifting a window while it is dragged. The numbers are the
// motion tokens of docs/design/motion/tokens.json (Dmytro Sizmin, CC-BY-SA-4.0):
// 150 / 280 / 450 ms, the exit factor 0.7, the reduced fade of 100 ms.
//
// Clutter's implicit animations take a named easing mode, not a cubic
// Bézier, so each token curve maps to the closest named mode:
//   ease (.22,1,.36,1)       -> easeOutQuint
//   ease-in (.4,0,1,1)       -> easeInCubic
//   ease-brush (.65,0,.35,1) -> easeInOutCubic
// Pure logic, no GNOME imports, so that node can test it.

export const MICRO = 150;
export const PANEL = 280;
export const DIALOG = 450;
export const EXIT_FACTOR = 0.7;
export const REDUCED_FADE = 100;

// Names of Clutter.AnimationMode values; windowMotion.js looks them up.
export const EASE = 'EASE_OUT_QUINT';
export const EASE_IN = 'EASE_IN_CUBIC';
export const EASE_BRUSH = 'EASE_IN_OUT_CUBIC';
export const EASE_MICRO = 'EASE_OUT_QUAD';

// Styles of the window-motion setting.
export const STYLES = ['vilna', 'reduced', 'gnome'];

export const OPEN_SCALE = 0.94;
export const CLOSE_SCALE = 0.96;
export const LIFT_SCALE = 1.01;

// What to do with one animation GNOME Shell is about to start on a
// normal window. event: 'map', 'destroy', 'minimize' or 'unminimize';
// shell: the parameters GNOME Shell passes to ease() (its targets are
// kept for minimising, where it aims at the app's icon in the dock).
// Returns null to leave GNOME's animation as it is, or
// {start, params}: properties to set on the window first (may be null)
// and the parameters to ease with instead.
export function motionFor(event, style, shell = {}) {
    if (style === 'gnome' || !STYLES.includes(style))
        return null;
    const callbacks = {};
    for (const key of ['onStopped', 'onComplete'])
        if (shell[key])
            callbacks[key] = shell[key];

    if (style === 'reduced') {
        // No movement: only a short fade, and minimising lands in place.
        switch (event) {
        case 'map':
            return {start: {opacity: 0, scale_x: 1, scale_y: 1},
                params: {...callbacks, opacity: 255, scale_x: 1, scale_y: 1, duration: REDUCED_FADE, mode: EASE_MICRO}};
        case 'destroy':
        case 'minimize':
            return {start: {scale_x: 1, scale_y: 1},
                params: {...callbacks, opacity: 0, duration: REDUCED_FADE, mode: EASE_MICRO}};
        case 'unminimize':
            // GNOME has put the window on the dock icon already: put it
            // back in its place and fade it in there.
            return {start: {...pick(shell, ['x', 'y']), opacity: 0, scale_x: 1, scale_y: 1},
                params: {...callbacks, opacity: 255, duration: REDUCED_FADE, mode: EASE_MICRO}};
        default:
            return null;
        }
    }

    switch (event) {
    case 'map':
        return {start: {pivot: [0.5, 0.5], opacity: 0, scale_x: OPEN_SCALE, scale_y: OPEN_SCALE},
            params: {...callbacks, opacity: 255, scale_x: 1, scale_y: 1, duration: PANEL, mode: EASE}};
    case 'destroy':
        return {start: {pivot: [0.5, 0.5]},
            params: {...callbacks, opacity: 0, scale_x: CLOSE_SCALE, scale_y: CLOSE_SCALE,
                duration: Math.round(PANEL * EXIT_FACTOR), mode: EASE_IN}};
    case 'minimize':
    case 'unminimize':
        // GNOME aims at the window's icon geometry, which Dash to Dock
        // sets to the app's icon: keep its targets, give it our timing.
        return {start: null,
            params: {...shell, ...callbacks, duration: DIALOG, mode: EASE_BRUSH}};
    default:
        return null;
    }
}

// Lifting a window while it is dragged: the scale and timing for the
// start ('begin') and the end ('end') of the drag, or null.
export function liftFor(phase, style) {
    if (style !== 'vilna')
        return null;
    const scale = phase === 'begin' ? LIFT_SCALE : 1;
    if (phase !== 'begin' && phase !== 'end')
        return null;
    return {scale_x: scale, scale_y: scale, duration: MICRO, mode: EASE_MICRO};
}

// Where the lift pivots: the pointer's place in the window, clamped to
// it, so that the spot under the pointer stays under the pointer.
export function liftPivot(pointer, rect) {
    const [px, py] = pointer;
    const {x, y, width, height} = rect;
    if (!(width > 0 && height > 0))
        return [0.5, 0.5];
    const clamp = v => Math.min(1, Math.max(0, v));
    return [clamp((px - x) / width), clamp((py - y) / height)];
}

function pick(object, keys) {
    const out = {};
    for (const key of keys)
        if (key in object)
            out[key] = object[key];
    return out;
}

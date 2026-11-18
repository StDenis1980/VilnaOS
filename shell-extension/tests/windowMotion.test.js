// SPDX-License-Identifier: GPL-3.0-or-later
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {
    DIALOG, EASE, EASE_BRUSH, EASE_IN, EASE_MICRO, PANEL, REDUCED_FADE,
    liftFor, liftPivot, motionFor,
} from '../vilna@stdenis1980.github.io/lib/windowMotion.js';

const done = () => {};

test('opening: from 0.94 and transparent, 280 ms, the ease curve', () => {
    const m = motionFor('map', 'vilna', {opacity: 255, scale_x: 1, scale_y: 1, duration: 150, onStopped: done});
    assert.deepEqual(m.start, {pivot: [0.5, 0.5], opacity: 0, scale_x: 0.94, scale_y: 0.94});
    assert.equal(m.params.duration, PANEL);
    assert.equal(m.params.mode, EASE);
    assert.deepEqual([m.params.opacity, m.params.scale_x, m.params.scale_y], [255, 1, 1]);
    assert.equal(m.params.onStopped, done, 'GNOME bookkeeping callback kept');
});

test('closing: 0.7 of opening, the ease-in curve, shrinks only a little', () => {
    const m = motionFor('destroy', 'vilna', {opacity: 0, scale_x: 0.8, scale_y: 0.8, onStopped: done});
    assert.equal(m.params.duration, 196);
    assert.equal(m.params.mode, EASE_IN);
    assert.deepEqual([m.params.opacity, m.params.scale_x], [0, 0.96]);
    assert.equal(m.params.onStopped, done);
});

test('minimising keeps GNOME\'s target (the dock icon), 450 ms brush', () => {
    const shell = {opacity: 0, x: 900, y: 1040, scale_x: 0.05, scale_y: 0.07, duration: 400, onStopped: done};
    for (const event of ['minimize', 'unminimize']) {
        const m = motionFor(event, 'vilna', shell);
        assert.equal(m.start, null);
        assert.deepEqual([m.params.x, m.params.y, m.params.scale_x, m.params.scale_y], [900, 1040, 0.05, 0.07]);
        assert.equal(m.params.duration, DIALOG);
        assert.equal(m.params.mode, EASE_BRUSH);
        assert.equal(m.params.onStopped, done);
    }
});

test('reduced motion: a 100 ms fade, no scaling or flying', () => {
    const map = motionFor('map', 'reduced', {onStopped: done});
    assert.deepEqual([map.start.scale_x, map.params.scale_x, map.params.duration, map.params.mode], [1, 1, REDUCED_FADE, EASE_MICRO]);
    const min = motionFor('minimize', 'reduced', {x: 900, y: 1040, scale_x: 0.05, opacity: 0, onStopped: done});
    assert.equal('x' in min.params, false, 'stays in place');
    assert.equal('scale_x' in min.params, false);
    assert.deepEqual([min.params.opacity, min.params.duration], [0, REDUCED_FADE]);
    const back = motionFor('unminimize', 'reduced', {x: 300, y: 200, scale_x: 1, onStopped: done});
    assert.deepEqual(back.start, {x: 300, y: 200, opacity: 0, scale_x: 1, scale_y: 1});
    assert.deepEqual([back.params.opacity, back.params.duration, back.params.onStopped], [255, REDUCED_FADE, done]);
});

test('GNOME style, unknown style or other events leave GNOME\'s motion', () => {
    assert.equal(motionFor('map', 'gnome', {}), null);
    assert.equal(motionFor('map', 'bouncy', {}), null);
    assert.equal(motionFor('size-change', 'vilna', {}), null);
    assert.equal(motionFor('size-change', 'reduced', {}), null);
});

test('a dragged window lifts to 1.01 and settles back, only in the Vilna style', () => {
    assert.deepEqual(liftFor('begin', 'vilna'), {scale_x: 1.01, scale_y: 1.01, duration: 150, mode: EASE_MICRO});
    assert.deepEqual(liftFor('end', 'vilna'), {scale_x: 1, scale_y: 1, duration: 150, mode: EASE_MICRO});
    assert.equal(liftFor('begin', 'reduced'), null);
    assert.equal(liftFor('begin', 'gnome'), null);
    assert.equal(liftFor('middle', 'vilna'), null);
});

test('the lift pivots at the pointer, clamped to the window', () => {
    const rect = {x: 100, y: 50, width: 400, height: 200};
    assert.deepEqual(liftPivot([300, 60], rect), [0.5, 0.05]);
    assert.deepEqual(liftPivot([0, 0], rect), [0, 0]);
    assert.deepEqual(liftPivot([900, 900], rect), [1, 1]);
    assert.deepEqual(liftPivot([1, 1], {x: 0, y: 0, width: 0, height: 0}), [0.5, 0.5]);
});

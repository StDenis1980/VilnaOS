// SPDX-License-Identifier: GPL-3.0-or-later
import test from 'node:test';
import assert from 'node:assert/strict';

import {Jelly, MAX_OFFSET, wobbles} from '../vilna@stdenis1980.github.io/lib/wobble.js';

const run = (jelly, seconds) => {
    for (let t = 0; t < seconds; t += 1 / 60)
        jelly.step(1 / 60);
};

test('the grabbed spot follows the pointer, the far corner lags', () => {
    const jelly = new Jelly();
    jelly.grab(0, 0);
    jelly.move(20, 0);
    assert.ok(Math.abs(jelly.at(0, 0)[0]) < 1, 'grab point nearly still');
    const [far] = jelly.at(1, 1);
    assert.ok(far < -10, `far corner lags behind the move: ${far}`);
    assert.equal(jelly.at(1, 1)[1], 0);
});

test('the springs settle back with a small overshoot', () => {
    const jelly = new Jelly();
    jelly.grab(0.5, 0);
    jelly.move(-30, 0);
    const start = jelly.at(0.5, 1)[0];
    assert.ok(start > 0);
    let least = start;
    for (let t = 0; t < 0.6; t += 1 / 60) {
        jelly.step(1 / 60);
        least = Math.min(least, jelly.at(0.5, 1)[0]);
    }
    assert.ok(least < 0, 'swings past rest once');
    assert.ok(least > -start / 2, 'but softly');
    run(jelly, 2);
    assert.ok(jelly.settled());
});

test('offsets stay bounded and hostile numbers do no harm', () => {
    const jelly = new Jelly();
    jelly.grab(NaN, Infinity);
    jelly.move(1e9, -1e9);
    for (const [u, v] of [[0, 0], [1, 1], [0.3, 0.7]]) {
        const [x, y] = jelly.at(u, v);
        assert.ok(Math.abs(x) <= MAX_OFFSET && Math.abs(y) <= MAX_OFFSET);
    }
    jelly.move(NaN, NaN);
    jelly.step(NaN);
    jelly.step(1e6); // one long frame is cut short, not exploded
    assert.ok(jelly.at(2, -1).every(Number.isFinite));
    jelly.reset();
    assert.ok(jelly.settled());
});

test('only Vilna motion with the setting on wobbles', () => {
    assert.equal(wobbles('vilna', true), true);
    assert.equal(wobbles('vilna', false), false);
    assert.equal(wobbles('reduced', true), false);
    assert.equal(wobbles('gnome', true), false);
});

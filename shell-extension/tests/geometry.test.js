// SPDX-License-Identifier: GPL-3.0-or-later
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {dockStrip, dockWidth, indicatorPosition, monitorAt} from '../vilna@stdenis1980.github.io/lib/geometry.js';

const fhd = {x: 0, y: 0, width: 1920, height: 1080};
const second = {x: 1920, y: 0, width: 1280, height: 1024};

test('the monitor under a point', () => {
    assert.equal(monitorAt([fhd, second], 2000, 10), second);
    assert.equal(monitorAt([fhd, second], -5, -5), fhd);
    assert.equal(monitorAt([], 0, 0), null);
});

test('the indicator sits below the cursor', () => {
    const cursor = {x: 500, y: 300, width: 1, height: 20};
    assert.deepEqual(indicatorPosition(cursor, {width: 40, height: 24}, fhd), {x: 500, y: 326});
});

test('the indicator stays on screen', () => {
    const size = {width: 40, height: 24};
    // Bottom edge: it goes above the cursor.
    assert.deepEqual(indicatorPosition({x: 10, y: 1070, width: 1, height: 8}, size, fhd), {x: 10, y: 1040});
    // Right edge.
    assert.deepEqual(indicatorPosition({x: 1910, y: 100, width: 1, height: 20}, size, fhd), {x: 1880, y: 126});
});

test('junk cursor values do not throw', () => {
    const p = indicatorPosition({x: NaN, y: Infinity}, {width: -3}, fhd);
    assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y));
    assert.deepEqual(indicatorPosition(null, null, null), {x: 0, y: 6});
});

test('strip width: a sixth or a third, at least 320 px, at most half', () => {
    assert.equal(dockWidth(1920, 'one-sixth'), 320);
    assert.equal(dockWidth(3840, 'one-sixth'), 640);
    assert.equal(dockWidth(1920, 'one-third'), 640);
    assert.equal(dockWidth(1366, 'one-sixth'), 320);
    assert.equal(dockWidth(500, 'one-sixth'), 250);
    assert.equal(dockWidth(1920, 'nonsense'), 320);
});

test('the strip starts below the top bar at the right edge', () => {
    const workArea = {x: 0, y: 32, width: 1920, height: 1048};
    assert.deepEqual(dockStrip(fhd, workArea, 'one-third'), {x: 1280, y: 32, width: 640, height: 1048});
});

test('strip on a second monitor, without or with a strange work area', () => {
    assert.deepEqual(dockStrip(second, null, 'one-sixth'), {x: 2880, y: 0, width: 320, height: 1024});
    assert.deepEqual(dockStrip(second, {x: 0, y: 5000, width: 1, height: 1}), {x: 2880, y: 0, width: 320, height: 1024});
    assert.equal(dockStrip(null, null), null);
    assert.equal(dockStrip({x: 0, y: 0, width: 0, height: 0}, null), null);
});

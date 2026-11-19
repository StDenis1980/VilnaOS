// SPDX-License-Identifier: GPL-3.0-or-later
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {
    PANEL_COMPACT_HEIGHT, PANEL_MIN_HEIGHT, dockStrip, dockWidth, indicatorPosition, monitorAt, panelRect,
} from '../vilna@stdenis1980.github.io/lib/geometry.js';

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

// The work area of a 1920x1080 monitor under a 32 px top bar.
const wa = {x: 0, y: 32, width: 1920, height: 1048};

test('panel: a sixth or a third, the whole height, at the right edge', () => {
    assert.deepEqual(panelRect(wa, 'one-sixth', null), {x: 1592, y: 40, width: 320, height: 1032});
    assert.deepEqual(panelRect(wa, 'one-third', null), {x: 1272, y: 40, width: 640, height: 1032});
});

test('panel: compact sits at the bottom right', () => {
    assert.deepEqual(panelRect(wa, 'compact', {width: 900, height: 900}),
        {x: 1592, y: 1080 - 8 - PANEL_COMPACT_HEIGHT, width: 320, height: PANEL_COMPACT_HEIGHT});
});

test('panel: custom keeps the window size, clamped to the screen', () => {
    assert.deepEqual(panelRect(wa, 'custom', {width: 500, height: 700}), {x: 1412, y: 40, width: 500, height: 700});
    assert.deepEqual(panelRect(wa, 'custom', {width: 5000, height: 5000}), {x: 8, y: 40, width: 1904, height: 1032});
    assert.equal(panelRect(wa, 'custom', {width: 400, height: 3}).height, PANEL_MIN_HEIGHT);
});

test('panel: a second monitor to the right', () => {
    const r = panelRect({x: 1920, y: 0, width: 1280, height: 1024}, 'one-sixth', null);
    assert.deepEqual(r, {x: 1920 + 1280 - 8 - 320, y: 8, width: 320, height: 1008});
});

test('panel: junk does not throw and stays finite', () => {
    for (const args of [[null, null, null], [{width: NaN, height: -1}, 'what', {width: Infinity}],
        [{x: 0, y: 0, width: 10, height: 10}, 'compact', null]]) {
        const r = panelRect(...args);
        for (const v of Object.values(r))
            assert.ok(Number.isFinite(v), JSON.stringify(args));
        assert.ok(r.width >= 1 && r.height >= 1);
    }
    // An unknown size falls back to a sixth.
    assert.deepEqual(panelRect(wa, 'half', null), panelRect(wa, 'one-sixth', null));
});

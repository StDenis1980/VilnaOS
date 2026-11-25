// SPDX-License-Identifier: GPL-3.0-or-later
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    FPS, MAX_PARTICLES, newSky, seeded, shapeCount, skyColours, skyShapes, stepSky,
} from '../vilna@stdenis1980.github.io/lib/weatherSky.js';

const KINDS = ['clear', 'cloudy', 'overcast', 'fog', 'drizzle', 'rain', 'snow', 'storm', 'wind', 'heat', 'hail', 'ice'];
const W = 340;
const H = 130;

const particles = sky => sky.drops.length + sky.flakes.length + sky.clouds.length + sky.fog.length +
    sky.streaks.length + sky.stars.length + sky.shimmer.length + sky.pellets.length + sky.glints.length;

test('every kind of weather has its own sky, readable by day and by night', () => {
    const seen = new Set();
    for (const kind of KINDS) {
        for (const day of [true, false]) {
            const {top, bottom, ink} = skyColours(kind, day);
            assert.ok([top, bottom].every(c => c.length === 3 && c.every(v => v >= 0 && v <= 255)), kind);
            // The text colour matches how light the sky is: dark ink on light skies.
            const light = c => (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
            const mid = (light(top) + light(bottom)) / 2;
            assert.equal(ink, mid > 0.5 ? 'dark' : 'light', `${kind} ${day ? 'day' : 'night'} ${mid}`);
            if (day)
                seen.add(`${top}|${bottom}`);
        }
    }
    assert.equal(seen.size, KINDS.length, 'no two kinds share a sky by day');
    assert.deepEqual(skyColours('nonsense'), skyColours('overcast'));
});

test('each sky has the motion of its weather, and few enough shapes for weak graphics', () => {
    const r = seeded(7);
    const sky = kind => newSky(kind, true, W, H, r);
    assert.ok(sky('rain').drops.length > 0 && sky('rain').flakes.length === 0);
    assert.ok(sky('storm').drops.length > sky('rain').drops.length);
    assert.ok(sky('drizzle').drops.length < sky('rain').drops.length);
    assert.ok(sky('snow').flakes.length > 0 && sky('snow').drops.length === 0);
    assert.ok(sky('fog').fog.length > 0);
    assert.ok(sky('wind').streaks.length > 0);
    assert.ok(sky('heat').shimmer.length > 0);
    assert.equal(sky('clear').stars.length, 0, 'no stars by day');
    assert.ok(newSky('clear', false, W, H, r).stars.length > 0, 'stars at night');
    assert.ok(sky('hail').pellets.length > 0, 'hailstones');
    assert.ok(sky('ice').glints.length > 0 && sky('ice').drops.length > 0, 'freezing rain and its glaze');
    const heavy = kind => newSky(kind, true, W, H, r, 2);
    const light = kind => newSky(kind, true, W, H, r, 0);
    assert.ok(light('rain').drops.length < sky('rain').drops.length && sky('rain').drops.length < heavy('rain').drops.length);
    assert.ok(light('snow').flakes.length < heavy('snow').flakes.length);
    for (const kind of KINDS) {
        for (const day of [true, false]) {
            for (const strength of [0, 1, 2]) {
                const s = newSky(kind, day, W, H, r, strength);
                assert.ok(particles(s) <= MAX_PARTICLES, `${kind} ${strength}`);
                for (let i = 0; i < 20 * FPS; i++) {
                    stepSky(s, 1 / FPS);
                    assert.ok(skyShapes(s).length <= shapeCount(s), kind);
                }
            }
        }
    }
    assert.ok(FPS > 0 && FPS <= 30);
});

test('a minute of motion keeps every particle on the card, and stays the same with the same seed', () => {
    for (const kind of KINDS) {
        const a = newSky(kind, true, W, H, seeded(3));
        const b = newSky(kind, true, W, H, seeded(3));
        for (let i = 0; i < 60 * FPS; i++) {
            stepSky(a, 1 / FPS);
            stepSky(b, 1 / FPS);
        }
        assert.deepEqual(skyShapes(a), skyShapes(b), kind);
        for (const p of [...a.drops, ...a.flakes, ...a.pellets])
            assert.ok(p.x > -40 && p.x < W + 40 && p.y > -40 && p.y < H + 40, `${kind} ${p.x},${p.y}`);
        for (const c of a.clouds)
            assert.ok(c.x > -150 && c.x < W + 150, kind);
        for (const s of skyShapes(a))
            for (const v of [s.x, s.y, s.r, ...(s.points ?? []).flat()].filter(v => v !== undefined))
                assert.ok(Number.isFinite(v), kind);
    }
});

test('rain and snow move, and a long pause does not make the sky jump', () => {
    const sky = newSky('rain', true, W, H, seeded(5));
    const y = sky.drops[0].y;
    stepSky(sky, 0.05);
    assert.notEqual(sky.drops[0].y, y);
    const t = sky.t;
    stepSky(sky, 3600);
    assert.equal(sky.t - t, 0.25);
    for (const bad of [NaN, -1, Infinity, 0])
        assert.equal(stepSky(sky, bad).t, sky.t);
});

test('a storm flashes now and then', () => {
    const sky = newSky('storm', false, W, H, seeded(9));
    let flashes = 0;
    for (let i = 0; i < 60 * FPS; i++) {
        stepSky(sky, 1 / FPS);
        if (skyShapes(sky).some(s => s.op === 'flash'))
            flashes++;
    }
    assert.ok(flashes > 0 && flashes < 60 * FPS / 4, String(flashes));
    assert.ok(!skyShapes(newSky('rain', true, W, H, seeded(9))).some(s => s.op === 'flash'));
});

test('a clear night visibly twinkles, and a star falls now and then', () => {
    const sky = newSky('clear', false, W, H, seeded(4));
    const alphas = () => skyShapes(sky).filter(s => s.op === 'circle').map(s => s.colour[3]);
    const before = alphas();
    stepSky(sky, 1);
    const after = alphas();
    const change = Math.max(...before.map((a, i) => Math.abs(a - after[i])));
    assert.ok(change > 0.3, `the stars change by ${change} in a second`);
    let meteors = 0;
    for (let i = 0; i < 60 * FPS; i++) {
        stepSky(sky, 1 / FPS);
        if (sky.meteor)
            meteors++;
    }
    assert.ok(meteors > 0, 'a falling star within a minute');
    assert.equal(newSky('clear', true, W, H, seeded(4)).meteor, null);
});

test('hailstones land and come back from the top', () => {
    const sky = newSky('hail', true, W, H, seeded(2));
    let bounced = false;
    for (let i = 0; i < 10 * FPS; i++) {
        stepSky(sky, 1 / FPS);
        bounced ||= sky.pellets.some(p => p.bounce > 0);
        for (const p of sky.pellets)
            assert.ok(p.y <= H, String(p.y));
    }
    assert.ok(bounced);
});

test('junk sizes give a tiny sky, never a crash or a huge one', () => {
    for (const [w, h] of [[0, 0], [-5, NaN], [1e9, 1e9], ['x', null]]) {
        const sky = newSky('snow', true, w, h, seeded(1));
        assert.ok(sky.w >= 1 && sky.w <= 4000 && sky.h >= 1 && sky.h <= 4000);
        stepSky(sky, 0.1);
        assert.ok(skyShapes(sky).length > 0);
    }
    assert.equal(newSky('tornado', true, W, H, seeded(1)).kind, 'overcast');
});

// SPDX-License-Identifier: GPL-3.0-or-later
//
// The live sky behind the weather card (docs/design/desktop/README.md,
// (з)): a gradient for each kind of weather and a little motion — rain,
// snow, hail, ice, fog, clouds, wind, stars, the sun and its heat. Pure: sky.js
// draws the shapes this returns with Cairo, and the tests and the
// mockup frames draw them the same way.
//
// The colours are Vilna's own (docs/design/brief.md): night blue,
// cream, gold and sky blue, darkened or greyed for bad weather.

const SKIES = {
    // kind: [top, bottom, ink] by day; ink is the text colour on it.
    clear: [[0x5d, 0xa9, 0xf2], [0xc6, 0xe5, 0xff], 'dark'],
    cloudy: [[0x7f, 0xb4, 0xe3], [0xd9, 0xe8, 0xf5], 'dark'],
    overcast: [[0x93, 0xa1, 0xb3], [0xcf, 0xd6, 0xdf], 'dark'],
    fog: [[0xb9, 0xc0, 0xc8], [0xe8, 0xea, 0xec], 'dark'],
    drizzle: [[0x7b, 0x8c, 0xa1], [0xb7, 0xc3, 0xd0], 'dark'],
    rain: [[0x45, 0x55, 0x6b], [0x7c, 0x8a, 0x9c], 'light'],
    snow: [[0xa9, 0xb9, 0xcc], [0xec, 0xf1, 0xf6], 'dark'],
    storm: [[0x1c, 0x23, 0x31], [0x3d, 0x48, 0x5b], 'light'],
    wind: [[0x6f, 0xaa, 0xda], [0xd2, 0xe5, 0xf3], 'dark'],
    heat: [[0xf2, 0x9a, 0x3a], [0xff, 0xe0, 0x9c], 'dark'],
    hail: [[0x2a, 0x33, 0x42], [0x5a, 0x68, 0x7a], 'light'],
    ice: [[0x46, 0x5c, 0x74], [0x7d, 0x92, 0xa8], 'light'],
};
const NIGHT = [[0x0e, 0x1a, 0x2e], [0x1f, 0x34, 0x5c]]; // night blue, docs/design/brief.md

// Most particles any sky has, so a frame stays cheap on weak graphics.
export const MAX_PARTICLES = 120;
// Frames per second while the sky moves.
export const FPS = 24;
const RADIUS = 20;

const mix = (a, b, f) => a.map((v, i) => Math.round(v + (b[i] - v) * f));
const rgba = (rgb, a) => [rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, a];
const finite = v => typeof v === 'number' && Number.isFinite(v);

// The gradient and the text colour of a kind, by day or night. At night
// a clear or cloudy sky is night blue; the others are darkened towards it.
export function skyColours(kind, day = true) {
    const [top, bottom, ink] = SKIES[kind] ?? SKIES.overcast;
    if (day)
        return {top, bottom, ink};
    if (kind === 'clear' || kind === 'cloudy' || kind === 'heat' || kind === 'wind')
        return {top: NIGHT[0], bottom: NIGHT[1], ink: 'light'};
    return {top: mix(top, NIGHT[0], 0.6), bottom: mix(bottom, NIGHT[1], 0.6), ink: 'light'};
}

// A small deterministic random source, so that tests and mockups repeat.
export function seeded(seed = 1) {
    let s = (seed >>> 0) || 1;
    return () => {
        s ^= s << 13;
        s ^= s >>> 17;
        s ^= s << 5;
        return (s >>> 0) / 4294967296;
    };
}

const COUNTS = {
    // kind: {drops, flakes, clouds, fog, streaks, shimmer, pellets, glints}
    // for moderate weather; light and heavy scale the drops, flakes and
    // pellets (STRENGTH).
    clear: {},
    cloudy: {clouds: 3},
    overcast: {clouds: 5},
    fog: {fog: 6},
    drizzle: {clouds: 4, drops: 30},
    rain: {clouds: 4, drops: 70},
    snow: {clouds: 2, flakes: 50},
    storm: {clouds: 5, drops: 100},
    wind: {clouds: 2, streaks: 12},
    heat: {shimmer: 4},
    hail: {clouds: 5, drops: 40, pellets: 40},
    ice: {clouds: 4, drops: 50, glints: 7},
};
const STRENGTH = [0.45, 1, 1.5];
// Stars on a clear or partly cloudy night; a few are bright and glint.
const STARS = 34;
const BRIGHT_STARS = 5;
// [dx, dy, r, alpha] from the moon's centre (radius 14).
const MOON_SPOTS = [[-4, -4, 4.2, 0.32], [3, -1, 3.2, 0.26], [-1, 5, 2.6, 0.28], [6, 6, 1.4, 0.35], [-7, 2, 1.2, 0.3]];

// A new sky for a card of w x h pixels.
// strength: 0 light, 1 moderate, 2 heavy (lib/widgets.js weatherKind).
export function newSky(kind, day, w, h, random = Math.random, strength = 1) {
    w = finite(w) && w > 0 ? Math.min(w, 4000) : 1;
    h = finite(h) && h > 0 ? Math.min(h, 4000) : 1;
    const known = COUNTS[kind] ? kind : 'overcast';
    const n = {...COUNTS[known]};
    const f = STRENGTH[strength] ?? 1;
    for (const key of ['drops', 'flakes', 'pellets'])
        if (n[key])
            n[key] = Math.round(n[key] * f);
    // Heavy weather still stays within MAX_PARTICLES: the drops give way.
    const others = (n.flakes ?? 0) + (n.clouds ?? 0) + (n.fog ?? 0) + (n.streaks ?? 0) + (n.shimmer ?? 0) +
        (n.pellets ?? 0) + (n.glints ?? 0);
    if (n.drops)
        n.drops = Math.max(0, Math.min(n.drops, MAX_PARTICLES - others));
    const r = random;
    const sky = {kind: known, day: day !== false, w, h, t: 0, random, flash: 0, nextFlash: 4 + r() * 6,
        drops: [], flakes: [], clouds: [], fog: [], streaks: [], stars: [], shimmer: [], pellets: [], glints: [],
        meteor: null, nextMeteor: 8 + r() * 12};
    for (let i = 0; i < (n.drops ?? 0); i++)
        sky.drops.push({x: r() * w, y: r() * h, len: 8 + r() * 10, speed: (known === 'drizzle' ? 220 : 520) + r() * 200});
    for (let i = 0; i < (n.flakes ?? 0); i++)
        sky.flakes.push({x: r() * w, y: r() * h, size: 1.2 + r() * 2.2, speed: 18 + r() * 26, phase: r() * 6.28});
    for (let i = 0; i < (n.clouds ?? 0); i++)
        sky.clouds.push({x: r() * w, y: h * (0.06 + r() * 0.3), scale: 0.55 + r() * 0.6, speed: 4 + r() * 8});
    for (let i = 0; i < (n.fog ?? 0); i++)
        sky.fog.push({x: r() * w, y: h * (i + 0.5) / n.fog, width: w * (0.6 + r() * 0.6), speed: 5 + r() * 10});
    for (let i = 0; i < (n.streaks ?? 0); i++)
        sky.streaks.push({x: r() * w, y: h * (0.1 + r() * 0.8), len: 40 + r() * 60, speed: 160 + r() * 140,
            phase: r() * 6.28});
    for (let i = 0; i < (n.shimmer ?? 0); i++)
        sky.shimmer.push({y: h * (0.55 + 0.12 * i), phase: r() * 6.28});
    for (let i = 0; i < (n.pellets ?? 0); i++)
        sky.pellets.push({x: r() * w, y: r() * h, size: 1.6 + r() * 1.6, speed: 420 + r() * 220, bounce: 0});
    // Glints of ice along the bottom of the card, each lighting up in turn.
    for (let i = 0; i < (n.glints ?? 0); i++)
        sky.glints.push({x: RADIUS + (w - 2 * RADIUS) * (i + 0.3 + r() * 0.4) / n.glints, y: h - 5 - r() * 2, phase: r() * 6.28});
    if (hasStars(sky)) {
        for (let i = 0; i < STARS; i++)
            sky.stars.push({x: r() * w, y: r() * h * 0.85, size: i < BRIGHT_STARS ? 1.6 + r() * 0.6 : 0.8 + r() * 1.0,
                phase: r() * 6.28, speed: 1.2 + r() * 2.2, bright: i < BRIGHT_STARS});
    }
    return sky;
}

const hasStars = sky => !sky.day && (sky.kind === 'clear' || sky.kind === 'cloudy');

const wrap = (v, max, margin) => v > max + margin ? v - max - 2 * margin : v < -margin ? v + max + 2 * margin : v;

// Moves the sky on by dt seconds (at most a quarter of a second, so a
// paused sky does not jump).
export function stepSky(sky, dt) {
    if (!finite(dt) || dt <= 0)
        return sky;
    dt = Math.min(dt, 0.25);
    const {w, h, random} = sky;
    sky.t += dt;
    const slant = sky.kind === 'storm' ? 0.35 : 0.18;
    for (const d of sky.drops) {
        d.y += d.speed * dt;
        d.x += d.speed * slant * dt;
        if (d.y > h + d.len) {
            d.y -= h + 2 * d.len;
            d.x = random() * w;
        }
        d.x = wrap(d.x, w, 20);
    }
    for (const f of sky.flakes) {
        f.y += f.speed * dt;
        f.x += Math.sin(sky.t * 1.3 + f.phase) * 12 * dt;
        if (f.y > h + 4) {
            f.y = -4;
            f.x = random() * w;
        }
        f.x = wrap(f.x, w, 6);
    }
    for (const p of sky.pellets) {
        if (p.bounce > 0) {
            // A hailstone hops once on the ground, then falls in again from the top.
            p.bounce -= dt;
            p.y = h - 3 - Math.sin((1 - p.bounce / 0.3) * Math.PI) * 10;
            if (p.bounce <= 0) {
                p.bounce = 0;
                p.y = -p.size - random() * 20;
                p.x = random() * w;
            }
        } else {
            p.y += p.speed * dt;
            p.x += p.speed * 0.12 * dt;
            if (p.y >= h - 3) {
                p.y = h - 3;
                p.bounce = 0.3;
            }
        }
        p.x = wrap(p.x, w, 10);
    }
    if (hasStars(sky)) {
        // Now and then, rarely, a star falls across the sky: the first
        // within half a minute, then one every 20-45 s.
        if (sky.meteor) {
            sky.meteor.life -= dt;
            sky.meteor.x += sky.meteor.vx * dt;
            sky.meteor.y += sky.meteor.vy * dt;
            if (sky.meteor.life <= 0)
                sky.meteor = null;
        } else {
            sky.nextMeteor -= dt;
            if (sky.nextMeteor <= 0) {
                sky.meteor = {x: w * (0.1 + random() * 0.5), y: h * (0.05 + random() * 0.25), vx: 260, vy: 90,
                    life: 0.7, total: 0.7};
                sky.nextMeteor = 20 + random() * 25;
            }
        }
    }
    for (const c of sky.clouds)
        c.x = wrap(c.x + c.speed * (sky.kind === 'wind' ? 6 : 1) * dt, w, 70 * c.scale);
    for (const b of sky.fog)
        b.x = wrap(b.x + b.speed * dt, w, b.width / 2);
    for (const s of sky.streaks)
        s.x = wrap(s.x + s.speed * dt, w, s.len);
    if (sky.kind === 'storm' || sky.kind === 'hail') {
        sky.flash = Math.max(0, sky.flash - dt * 4);
        sky.nextFlash -= dt;
        if (sky.nextFlash <= 0) {
            sky.flash = 1;
            sky.nextFlash = 5 + random() * 8;
        }
    }
    return sky;
}

// The shapes of one frame, back to front:
//   {op: 'sky', top, bottom, radius}   the rounded card, a vertical gradient
//   {op: 'glow', x, y, r, colour}      a soft disc fading out
//   {op: 'circle', x, y, r, colour}
//   {op: 'cloud', circles: [[x, y, r]...], colour}  the circles filled as one
//   {op: 'line', points, width, colour}  a polyline, round caps
//   {op: 'flash', colour}             the whole card
// Colours are [r, g, b, a] from 0 to 1.
export function skyShapes(sky) {
    const {w, h, t} = sky;
    const {top, bottom, ink} = skyColours(sky.kind, sky.day);
    const shapes = [{op: 'sky', top: rgba(top, 1), bottom: rgba(bottom, 1), radius: RADIUS}];
    const light = ink === 'light';
    const sunny = sky.day && (sky.kind === 'clear' || sky.kind === 'cloudy' || sky.kind === 'heat' || sky.kind === 'wind');
    if (sunny) {
        const x = w - 52, y = 40;
        const big = sky.kind === 'heat' ? 1.35 : 1;
        shapes.push({op: 'glow', x, y, r: 70 * big, colour: [1, 0.86, 0.45, sky.kind === 'heat' ? 0.75 : 0.55]});
        const pulse = 1 + 0.06 * Math.sin(t * 1.6);
        shapes.push({op: 'circle', x, y, r: 20 * big * pulse, colour: [0.96, 0.76, 0.29, 1]}); // gold #F5C24A
    } else if (hasStars(sky)) {
        // Stars twinkle each at its own pace, from almost out to full;
        // the bright ones glint with a small cross. The moon breathes.
        for (const s of sky.stars) {
            const tw = 0.5 + 0.5 * Math.sin(t * s.speed + s.phase);
            const a = 0.15 + 0.85 * tw;
            shapes.push({op: 'circle', x: s.x, y: s.y, r: s.size * (0.8 + 0.3 * tw), colour: [0.95, 0.91, 0.84, a]}); // cream
            if (s.bright && tw > 0.6) {
                const l = s.size * 3.5 * tw;
                const c = [0.95, 0.91, 0.84, 0.6 * tw];
                shapes.push({op: 'line', points: [[s.x - l, s.y], [s.x + l, s.y]], width: 0.8, colour: c});
                shapes.push({op: 'line', points: [[s.x, s.y - l], [s.x, s.y + l]], width: 0.8, colour: c});
            }
        }
        if (sky.meteor) {
            const m = sky.meteor;
            const fade = m.life / m.total;
            shapes.push({op: 'line', points: [[m.x - 46, m.y - 16], [m.x, m.y]], width: 1.4,
                colour: [0.95, 0.91, 0.84, 0.85 * fade]});
        }
        const breath = 0.5 + 0.5 * Math.sin(t * 0.8);
        shapes.push({op: 'glow', x: w - 52, y: 40, r: 40 + 6 * breath, colour: [0.95, 0.91, 0.84, 0.2 + 0.12 * breath]});
        shapes.push({op: 'circle', x: w - 52, y: 40, r: 14, colour: [0.95, 0.91, 0.84, 0.95]});
        // The moon's seas and a crater or two, a little darker than it.
        for (const [dx, dy, r, a] of MOON_SPOTS)
            shapes.push({op: 'circle', x: w - 52 + dx, y: 40 + dy, r, colour: [0.62, 0.6, 0.58, a]});
    }
    for (const b of sky.shimmer) {
        const points = [];
        for (let x = 0; x <= w; x += 12)
            points.push([x, b.y + Math.sin(x / 22 + t * 2.2 + b.phase) * 2.5]);
        shapes.push({op: 'line', points, width: 1.5, colour: [1, 1, 1, 0.28]});
    }
    // Each cloud is one shape, so overlapping puffs do not show through
    // each other; light enough that the text over it still reads.
    const cloudColour = light ? [0.82, 0.86, 0.92, 0.22] : [1, 1, 1, sky.kind === 'overcast' ? 0.38 : 0.5];
    for (const c of sky.clouds) {
        const s = c.scale;
        const circles = [[-26, 6, 18], [0, -4, 26], [28, 6, 18], [8, 10, 20]]
            .map(([dx, dy, r]) => [c.x + dx * s, c.y + dy * s, r * s]);
        shapes.push({op: 'cloud', circles, colour: cloudColour});
    }
    for (const b of sky.fog)
        shapes.push({op: 'line', points: [[b.x - b.width / 2, b.y], [b.x + b.width / 2, b.y]], width: 14,
            colour: [1, 1, 1, 0.3]});
    for (const s of sky.streaks) {
        const points = [];
        for (let i = 0; i <= 6; i++) {
            const x = s.x - s.len + (s.len * i) / 6;
            points.push([x, s.y + Math.sin(x / 18 + s.phase) * 3]);
        }
        shapes.push({op: 'line', points, width: 1.6, colour: [1, 1, 1, 0.7]});
    }
    const slant = sky.kind === 'storm' || sky.kind === 'hail' ? 0.35 : 0.18;
    const dropColour = light ? [0.8, 0.88, 1, 0.4] : [0.18, 0.32, 0.55, 0.4];
    for (const d of sky.drops)
        shapes.push({op: 'line', points: [[d.x, d.y], [d.x - d.len * slant, d.y - d.len]], width: 1.3, colour: dropColour});
    for (const f of sky.flakes)
        shapes.push({op: 'circle', x: f.x, y: f.y, r: f.size, colour: [1, 1, 1, 0.9]});
    for (const p of sky.pellets)
        shapes.push({op: 'circle', x: p.x, y: p.y, r: p.size, colour: [0.93, 0.96, 1, 0.95]});
    if (sky.glints.length) {
        // A thin glaze of ice on the ground, and glints running along it.
        shapes.push({op: 'line', points: [[RADIUS, h - 4], [w - RADIUS, h - 4]], width: 3, colour: [0.86, 0.94, 1, 0.5]});
        for (const g of sky.glints) {
            const a = Math.max(0, Math.sin(t * 1.4 + g.phase));
            shapes.push({op: 'circle', x: g.x, y: g.y, r: 1.5 + 2.2 * a, colour: [1, 1, 1, 0.95 * a]});
        }
    }
    if (sky.flash > 0)
        shapes.push({op: 'flash', colour: [1, 1, 1, 0.55 * sky.flash]});
    return shapes;
}

// How many shapes a frame has at most, for the tests.
export function shapeCount(sky) {
    return sky.drops.length + sky.flakes.length + sky.clouds.length + sky.fog.length + sky.streaks.length +
        sky.stars.length * 3 + sky.shimmer.length + sky.pellets.length + sky.glints.length + MOON_SPOTS.length + 9;
}

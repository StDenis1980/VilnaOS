// SPDX-License-Identifier: GPL-3.0-or-later
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

import {
    DOCK_LANGUAGES, HIDE_MODES, ICON_SIZE_MAX, ICON_SIZE_MIN, checkDockTables, clampIconSize, dockStylesheet,
    dockText, glassAlpha, hideKeys, hideMode, iconMotion,
} from '../vilna@stdenis1980.github.io/lib/dock.js';

const alphas = (css, scheme, side) => {
    const block = css.split('\n\n').find(r => r.includes(`.vilna-dock.${scheme} #dashtodockContainer.${side} #dash .dash-background`));
    return [...(block ?? '').matchAll(/rgba\([^)]*, ([0-9.]+)\)/g)].map(m => Number(m[1]));
};

test('more transparency, less glass, on every side', () => {
    const solid = dockStylesheet(0);
    const half = dockStylesheet(50);
    const clear = dockStylesheet(100);
    for (const side of ['bottom', 'left', 'right', 'top']) {
        for (const scheme of ['vilna-light', 'vilna-dark']) {
            const [glassSolid, lineSolid] = alphas(solid, scheme, side);
            const [glassHalf, lineHalf] = alphas(half, scheme, side);
            assert.ok(glassSolid > glassHalf && glassHalf > 0, `${scheme} ${side}`);
            assert.ok(lineSolid > lineHalf, `${scheme} ${side} border`);
            // 100 %: only the icons, no glass and no edge.
            assert.deepEqual(alphas(clear, scheme, side), [0, 0]);
        }
    }
});

test('the selectors outrank Dash to Dock\'s own', () => {
    // Dash to Dock: #dashtodockContainer.bottom #dash .dash-background
    // (2 ids, 2 classes). Ours need more classes at the same ids.
    const selector = dockStylesheet(8).split('\n').find(l => l.includes('.dash-background {'));
    const ids = (selector.match(/#/g) ?? []).length;
    const classes = (selector.match(/\./g) ?? []).length;
    assert.equal(ids, 2);
    assert.ok(classes > 2);
});

test('junk transparency falls back or is clamped', () => {
    assert.ok(glassAlpha('x') > glassAlpha(50));
    assert.equal(glassAlpha(-5), glassAlpha(0));
    assert.equal(glassAlpha(500), glassAlpha(100));
});

test('icon size stays in its range', () => {
    assert.equal(clampIconSize(8), ICON_SIZE_MIN);
    assert.equal(clampIconSize(500), ICON_SIZE_MAX);
    assert.ok(clampIconSize('nope') >= ICON_SIZE_MIN && clampIconSize('nope') <= ICON_SIZE_MAX);
});

test('each hiding mode reads back as itself', () => {
    for (const mode of HIDE_MODES) {
        const keys = hideKeys(mode);
        assert.equal(hideMode({dockFixed: keys['dock-fixed'], intellihide: keys.intellihide}), mode);
    }
});

test('icons move only with the spring on and animations on', () => {
    assert.ok(iconMotion('hover').scale > 1);
    assert.ok(iconMotion('press').scale < 1);
    assert.ok(iconMotion('jiggle').rotation !== 0);
    for (const options of [{motion: 'off'}, {animations: false}, {reduced: true}]) {
        assert.equal(iconMotion('hover', options), null);
        assert.equal(iconMotion('jiggle', options), null);
        // Leaving puts a grown icon back at once.
        assert.deepEqual([iconMotion('leave', options).scale, iconMotion('leave', options).duration], [1, 0]);
    }
});

test('the Dock page speaks the ten languages of Vilna', () => {
    assert.deepEqual(checkDockTables(), []);
    assert.deepEqual([...DOCK_LANGUAGES].sort(), ['cs', 'de', 'en', 'es', 'fr', 'it', 'lt', 'pl', 'ro', 'uk']);
    assert.equal(dockText(['uk_UA.UTF-8', 'en']).page, 'Док');
    assert.equal(dockText(['xx']).page, 'Dock');
});

test('the schema has the keys the dock code reads', () => {
    const schema = fs.readFileSync(new URL('../vilna@stdenis1980.github.io/schemas/org.gnome.shell.extensions.vilna.gschema.xml', import.meta.url), 'utf8');
    for (const key of ['dock-transparency', 'dock-icon-motion'])
        assert.match(schema, new RegExp(`<key name="${key}"`));
});

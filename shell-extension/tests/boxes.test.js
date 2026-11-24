// SPDX-License-Identifier: GPL-3.0-or-later
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

import {verticalBoxParams} from '../vilna@stdenis1980.github.io/lib/boxes.js';

const VERTICAL = 1;

test('a vertical box names the property the running Shell has', () => {
    assert.deepEqual(verticalBoxParams({x_expand: true}, true, VERTICAL), {x_expand: true, orientation: VERTICAL});
    assert.deepEqual(verticalBoxParams({x_expand: true}, false, VERTICAL), {x_expand: true, vertical: true});
    assert.deepEqual(verticalBoxParams(undefined, true, VERTICAL), {orientation: VERTICAL});
    assert.deepEqual(verticalBoxParams({vertical: true, style_class: 'a'}, true, VERTICAL), {style_class: 'a', orientation: VERTICAL},
        'a stray «vertical» never reaches Shell 51');
});

test('Build12.26: no St.BoxLayout names «vertical» or «orientation» itself', () => {
    // On Shell 51 `vertical` throws and the whole extension goes to ERROR;
    // on Shell 46/47 `orientation` would. Only vbox.js chooses.
    const dir = new URL('../vilna@stdenis1980.github.io/', import.meta.url);
    const files = fs.readdirSync(dir).filter(name => name.endsWith('.js') && name !== 'vbox.js');
    const bad = [];
    for (const name of files) {
        const source = fs.readFileSync(new URL(name, dir), 'utf8');
        for (const [n, line] of source.split('\n').entries()) {
            if (/St\.BoxLayout\(\{[^}]*\b(vertical|orientation)\s*:/.test(line) || /\.vertical\s*=/.test(line))
                bad.push(`${name}:${n + 1}`);
        }
    }
    assert.deepEqual(bad, []);
});

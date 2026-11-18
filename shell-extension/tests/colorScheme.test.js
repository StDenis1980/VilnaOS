// SPDX-License-Identifier: GPL-3.0-or-later
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {DARK, LIGHT, schemeClass} from '../vilna@stdenis1980.github.io/lib/colorScheme.js';

test('Vilna is light unless the person prefers dark', () => {
    assert.equal(schemeClass('default'), LIGHT);
    assert.equal(schemeClass('prefer-light'), LIGHT);
    assert.equal(schemeClass('prefer-dark'), DARK);
});

test('an unknown or missing value falls back to light', () => {
    assert.equal(schemeClass(''), LIGHT);
    assert.equal(schemeClass(undefined), LIGHT);
    assert.equal(schemeClass('high-contrast'), LIGHT);
});

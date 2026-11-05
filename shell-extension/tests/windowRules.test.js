// SPDX-License-Identifier: GPL-3.0-or-later
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {GLOXI_APP_ID, wantsAbove, windowRole} from '../vilna@stdenis1980.github.io/lib/windowRules.js';

test('the Gloxi window', () => {
    assert.equal(windowRole([GLOXI_APP_ID, null]), 'gloxi');
    assert.equal(windowRole([null, GLOXI_APP_ID]), 'gloxi');
    assert.equal(windowRole(['IO.GITHUB.STDENIS1980.GLOXI']), 'gloxi');
});

test('the Herkos dialog is not mistaken for Gloxi', () => {
    assert.equal(windowRole([`${GLOXI_APP_ID}.HerkosDialog`]), 'herkos-dialog');
    assert.equal(windowRole(['io.github.stdenis1980.Herkos.Dialog']), 'herkos-dialog');
});

test('other windows and prefixes are left alone', () => {
    assert.equal(windowRole(['org.gnome.Nautilus']), null);
    assert.equal(windowRole([`${GLOXI_APP_ID}.Evil`]), null);
    assert.equal(windowRole(['io.github.stdenis1980']), null);
    assert.equal(windowRole([]), null);
    assert.equal(windowRole(undefined), null);
});

test('Gloxi follows the setting, Herkos is always above', () => {
    assert.equal(wantsAbove('gloxi', {gloxiKeepAbove: true}), true);
    assert.equal(wantsAbove('gloxi', {gloxiKeepAbove: false}), false);
    assert.equal(wantsAbove('herkos-dialog', {gloxiKeepAbove: false}), true);
    assert.equal(wantsAbove(null, {gloxiKeepAbove: true}), false);
});

test('dialogs follow their parent window', () => {
    assert.equal(wantsAbove('gloxi', {gloxiKeepAbove: true, transient: true}), false);
});

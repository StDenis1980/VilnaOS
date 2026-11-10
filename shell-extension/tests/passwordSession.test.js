// SPDX-License-Identifier: GPL-3.0-or-later
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {PasswordSession} from '../vilna@stdenis1980.github.io/lib/passwordSession.js';

const xkb = id => ({type: 'xkb', id});
const us = xkb('us'), ua = xkb('ua'), pl = xkb('pl');
const sources = [ua, us, pl];

test('switch on entering and back on leaving', () => {
    const s = new PasswordSession();
    assert.deepEqual(s.enter(sources, ua, ['us']), {action: 'switch', to: us});
    assert.ok(s.active);
    assert.deepEqual(s.leave(sources, us), {action: 'switch', to: ua});
    assert.ok(!s.active);
});

test('a manual switch inside the field still gives the old layout back', () => {
    const s = new PasswordSession();
    s.enter(sources, ua, ['us']);
    // The person picked Polish inside the field.
    assert.deepEqual(s.leave(sources, pl), {action: 'switch', to: ua});
});

test('password and repeat-password keep the original layout', () => {
    const s = new PasswordSession();
    s.enter(sources, ua, ['us']);
    assert.deepEqual(s.enter(sources, us, ['us']), {action: 'stay'});
    assert.deepEqual(s.leave(sources, us), {action: 'switch', to: ua});
});

test('nothing to restore when the layout never changed', () => {
    const s = new PasswordSession();
    assert.deepEqual(s.enter(sources, us, ['us']), {action: 'stay'});
    assert.deepEqual(s.leave(sources, us), {action: 'stay'});
});

test('Polish switches to English and comes back', () => {
    const s = new PasswordSession();
    assert.deepEqual(s.enter(sources, pl, ['us']), {action: 'switch', to: us});
    assert.deepEqual(s.leave(sources, us), {action: 'switch', to: pl});
});

test('a layout removed meanwhile is not restored', () => {
    const s = new PasswordSession();
    s.enter(sources, ua, ['us']);
    assert.deepEqual(s.leave([us, pl], us), {action: 'stay'});
});

test('leaving twice, or without entering, does nothing', () => {
    const s = new PasswordSession();
    assert.deepEqual(s.leave(sources, ua), {action: 'stay'});
    s.enter(sources, ua, ['us']);
    s.leave(sources, us);
    assert.deepEqual(s.leave(sources, us), {action: 'stay'});
});

test('no current source at all', () => {
    const s = new PasswordSession();
    assert.deepEqual(s.enter(sources, null, ['us']), {action: 'switch', to: us});
    assert.deepEqual(s.leave(sources, us), {action: 'stay'});
});

test('layouts reloaded inside the field: Latin again, original still given back', () => {
    const s = new PasswordSession();
    s.enter(sources, ua, ['us']);
    // GNOME reactivated Ukrainian after the list changed in Settings.
    assert.deepEqual(s.reapply(sources, ua, ['us']), {action: 'switch', to: us});
    assert.deepEqual(s.leave(sources, us), {action: 'switch', to: ua});
    assert.deepEqual(s.reapply(sources, ua, ['us']), {action: 'stay'});
});

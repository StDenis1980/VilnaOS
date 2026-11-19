// SPDX-License-Identifier: GPL-3.0-or-later
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {decideOnEnter, isLatin, nextSource, pickLatin} from '../vilna@stdenis1980.github.io/lib/inputSources.js';

const xkb = id => ({type: 'xkb', id});
const us = xkb('us'), ua = xkb('ua'), pl = xkb('pl'), gb = xkb('gb');

test('Latin and non-Latin layouts', () => {
    for (const id of ['us', 'gb', 'pl', 'de', 'fr', 'lt', 'cz', 'ro', 'es', 'it', 'us+colemak'])
        assert.ok(isLatin(xkb(id)), id);
    for (const id of ['ua', 'ua+phonetic', 'ru', 'by', 'bg', 'gr', 'rs', 'ge', 'il'])
        assert.ok(!isLatin(xkb(id)), id);
});

test('a latin variant of a Cyrillic layout is Latin', () => {
    assert.ok(isLatin(xkb('rs+latin')));
    assert.ok(isLatin(xkb('uz+latin')));
});

test('IBus engines and junk are never taken as Latin', () => {
    assert.ok(!isLatin({type: 'ibus', id: 'anthy'}));
    assert.ok(!isLatin(null));
    assert.ok(!isLatin({type: 'xkb'}));
    assert.ok(!isLatin(xkb('')));
    assert.ok(!isLatin(xkb('+latin')));
});

test('the preferred layout wins, in its order', () => {
    assert.equal(pickLatin([ua, pl, us], ['us']), us);
    assert.equal(pickLatin([ua, gb, us], ['gb', 'us']), gb);
});

test('without the preferred layout, the first Latin one is used', () => {
    assert.equal(pickLatin([ua, pl, gb], ['us']), pl);
    assert.equal(pickLatin([ua, xkb('ru')], ['us']), null);
    assert.equal(pickLatin(undefined, undefined), null);
});

test('Ukrainian switches to English', () => {
    assert.deepEqual(decideOnEnter([ua, us], ua, ['us']), {action: 'switch', to: us});
});

test('another Latin layout switches to English too', () => {
    assert.deepEqual(decideOnEnter([pl, ua, us], pl, ['us']), {action: 'switch', to: us});
});

test('already on English: stay', () => {
    assert.deepEqual(decideOnEnter([ua, us], us, ['us']), {action: 'stay'});
});

test('without English, a Latin layout is kept', () => {
    assert.deepEqual(decideOnEnter([pl, ua], pl, ['us']), {action: 'stay'});
    assert.deepEqual(decideOnEnter([ua, pl], ua, ['us']), {action: 'switch', to: pl});
});

test('no Latin layout at all gives a warning', () => {
    assert.deepEqual(decideOnEnter([ua], ua, ['us']), {action: 'no-latin'});
});

test('the indicator click goes round the list', () => {
    assert.equal(nextSource([ua, us], ua), us);
    assert.equal(nextSource([ua, us], us), ua);
    assert.equal(nextSource([ua, us], null), ua);
    assert.equal(nextSource([], ua), null);
});

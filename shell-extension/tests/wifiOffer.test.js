// SPDX-License-Identifier: GPL-3.0-or-later
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {LANGUAGES, MAX_REMEMBERED, WifiOffer, offerText} from '../vilna@stdenis1980.github.io/lib/wifiOffer.js';

const cafe = {uuid: 'cafe', wifi: true, vpnUp: false};

test('after the login page of a Wi-Fi network, once', () => {
    const o = new WifiOffer();
    assert.equal(o.update({...cafe, connectivity: 'limited'}), false);
    assert.equal(o.update({...cafe, connectivity: 'portal'}), false);
    assert.equal(o.update({...cafe, connectivity: 'full'}), true);
    assert.deepEqual(o.offered, ['cafe']);
    // The same cafe tomorrow: no second offer.
    o.update({...cafe, connectivity: 'portal'});
    assert.equal(o.update({...cafe, connectivity: 'full'}), false);
});

test('a network without a login page is left alone', () => {
    const o = new WifiOffer();
    assert.equal(o.update({...cafe, uuid: 'home', connectivity: 'full'}), false);
    assert.deepEqual(o.offered, []);
});

test('never while a VPN is up, and the network may be offered later', () => {
    const o = new WifiOffer();
    o.update({...cafe, connectivity: 'portal'});
    assert.equal(o.update({...cafe, connectivity: 'full', vpnUp: true}), false);
    o.update({...cafe, connectivity: 'portal'});
    assert.equal(o.update({...cafe, connectivity: 'full'}), true);
});

test('only Wi-Fi, and a login on one network does not count for another', () => {
    const o = new WifiOffer();
    o.update({...cafe, wifi: false, connectivity: 'portal'});
    assert.equal(o.update({...cafe, wifi: false, connectivity: 'full'}), false);
    o.update({...cafe, connectivity: 'portal'});
    assert.equal(o.update({...cafe, uuid: 'hotel', connectivity: 'full'}), false);
    assert.equal(o.update({uuid: null, wifi: true, connectivity: 'full'}), false);
});

test('remembers a bounded list and ignores junk from the settings', () => {
    const o = new WifiOffer(['', 7, null, 'old']);
    assert.deepEqual(o.offered, ['old']);
    for (let i = 0; i < MAX_REMEMBERED + 5; i++) {
        o.update({...cafe, uuid: `n${i}`, connectivity: 'portal'});
        o.update({...cafe, uuid: `n${i}`, connectivity: 'full'});
    }
    assert.equal(o.offered.length, MAX_REMEMBERED);
    assert.ok(!o.offered.includes('old'));
});

test('the words in all main languages, English otherwise', () => {
    assert.equal(LANGUAGES.length, 15);
    for (const lang of LANGUAGES) {
        const t = offerText([lang]);
        assert.ok(t.title && t.body && t.action, lang);
    }
    assert.equal(offerText(['uk_UA.UTF-8', 'uk', 'C']).action, 'Налаштувати VPN');
    assert.equal(offerText(['ja_JP.UTF-8', 'C']).title, 'Shared Wi-Fi');
    assert.equal(offerText(undefined).title, 'Shared Wi-Fi');
    assert.ok(!offerText(['en']).body.includes('Proton Drive'));
});

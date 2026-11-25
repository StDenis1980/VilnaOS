// SPDX-License-Identifier: GPL-3.0-or-later
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    CLOCK_LANGUAGES, checkClockTables, clockState, clockText, nextAlarm, nextRing, offsetText, whenKind, zoneRows,
} from '../vilna@stdenis1980.github.io/lib/clockInfo.js';
import {LANGUAGES} from '../vilna@stdenis1980.github.io/lib/widgets.js';

// Wednesday 25 November 2026, 22:25 local time.
const NOW = new Date(2026, 10, 25, 22, 25);

test('an alarm that is on rings at its ring time, even when snoozed off its hour', () => {
    const ring = new Date(2026, 10, 25, 22, 40);
    assert.equal(nextRing({hour: 22, minute: 30, ring_time: ring.toISOString()}, NOW).getTime(), ring.getTime());
});

test('ring times in the form GLib writes them are read', () => {
    const at = nextRing({hour: 7, minute: 0, ring_time: '2026-11-26T07:00:00+01'}, new Date('2026-11-25T21:25:00Z'));
    assert.equal(at?.toISOString(), '2026-11-26T06:00:00.000Z');
    assert.equal(nextRing({hour: 7, minute: 0, ring_time: '2026-11-26T07:00:00.000000+0530'}, NOW)?.toISOString(),
        '2026-11-26T01:30:00.000Z');
});

test('an alarm without a ring time is off, unless an old Clocks wrote active', () => {
    assert.equal(nextRing({hour: 7, minute: 0, days: [1, 2, 3, 4, 5]}, NOW), null);
    const legacy = nextRing({hour: 7, minute: 0, active: true}, NOW);
    assert.ok(legacy > NOW && legacy - NOW < 86400000, 'the next 7:00 after now');
});

test('a repeating alarm whose ring time went stale rings on its next weekday', () => {
    const stale = new Date(2026, 10, 20, 7, 0).toISOString();
    // Monday only: from a Wednesday evening that is five days on.
    const at = nextRing({hour: 7, minute: 0, days: [1], ring_time: stale}, NOW);
    assert.equal(at.getDay(), 1);
    assert.ok(at > NOW);
    assert.equal(whenKind(at, NOW), 'week');
});

test('a one-off alarm that already rang is not shown', () => {
    const past = new Date(2026, 10, 25, 7, 0).toISOString();
    assert.equal(nextRing({hour: 7, minute: 0, ring_time: past}, NOW), null);
});

test('junk alarms are skipped and the earliest one wins', () => {
    const tomorrow = new Date(2026, 10, 26, 7, 0);
    const later = new Date(2026, 10, 27, 6, 30);
    const next = nextAlarm([
        {hour: 99, minute: 0, ring_time: tomorrow.toISOString()},
        {hour: 6, minute: 30, ring_time: later.toISOString(), name: 'Early'},
        {hour: 7, minute: 0, ring_time: tomorrow.toISOString(), name: ' Work '},
        {hour: 'x', minute: 0, active: true},
        null,
    ], NOW);
    assert.deepEqual(next, {at: tomorrow, name: 'Work'});
    assert.equal(nextAlarm('nonsense', NOW), null);
    assert.equal(whenKind(tomorrow, NOW), 'tomorrow');
    assert.equal(whenKind(new Date(2026, 10, 25, 23, 0), NOW), 'today');
    assert.equal(whenKind(new Date(2026, 11, 9, 7, 0), NOW), 'date');
});

test('other zones: those with our time are left out, at most two are kept', () => {
    const rows = zoneRows([
        {name: 'Madrid', offset: 3600},
        {name: 'Київ', offset: 7200},
        {name: 'Київ', offset: 7200},
        {name: 'New York', offset: -18000},
        {name: 'Kolkata', offset: 19800},
    ], 3600);
    assert.deepEqual(rows.map(r => [r.name, r.diff]), [['Київ', '+1'], ['New York', '−6']]);
    assert.equal(offsetText(16200), '+4:30');
    assert.equal(offsetText(0), '');
    assert.deepEqual(zoneRows(null, 0), []);
});

test('only Clocks tabs can be chosen', () => {
    assert.equal(clockState('alarm'), "'alarm'");
    assert.equal(clockState("x'; evil"), null);
});

test('every widget language has every clock word', () => {
    assert.deepEqual(checkClockTables(), []);
    assert.deepEqual([...CLOCK_LANGUAGES].sort(), [...LANGUAGES].sort());
    for (const code of CLOCK_LANGUAGES) {
        const t = clockText([code]);
        assert.match(t.hours, /%s/, code);
        assert.ok(t.menu.startsWith(t.clock), `${code}: the menu name starts with the card's name`);
    }
    assert.equal(clockText(['sv_SE']).alarms, 'Alarms');
});

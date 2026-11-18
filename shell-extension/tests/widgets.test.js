// SPDX-License-Identifier: GPL-3.0-or-later
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    CAPITALS, LANGUAGES, askArgv, backupWhen, checkTables, countryOfLocale, countryOfZone, forecastUrl,
    geocodeUrl, homeCountry, lastBackup, parseForecast, parseGeocode, roundForPrivacy, todayEvents,
    weatherKind, weatherPlace, widgetText,
} from '../vilna@stdenis1980.github.io/lib/widgets.js';

const ZONES = '# comment\nES\t+4024-00341\tEurope/Madrid\tSpain (mainland)\nUA\t+5026+03031\tEurope/Kyiv\tmost of Ukraine\n';

test('the country comes from the time zone first, then the region, then the language', () => {
    assert.equal(countryOfZone('Europe/Madrid', ZONES), 'ES');
    assert.equal(countryOfZone('Mars/Base', ZONES), null);
    assert.equal(countryOfLocale('uk_UA.UTF-8'), 'UA');
    assert.equal(countryOfLocale('C'), null);
    assert.equal(homeCountry({zone: 'Europe/Madrid', zoneTab: ZONES, region: 'uk_UA.UTF-8'}), 'ES');
    assert.equal(homeCountry({zone: 'UTC', zoneTab: ZONES, region: 'pl_PL.UTF-8', lang: 'en_US.UTF-8'}), 'PL');
    assert.equal(homeCountry({zone: 'UTC', zoneTab: ZONES, region: '', lang: 'de_DE.UTF-8'}), 'DE');
    assert.equal(homeCountry({}), null);
});

test('by default the weather is for the capital; a chosen place wins', () => {
    assert.deepEqual(weatherPlace(['', 0, 0], 'ES'), {name: 'Madrid', lat: 40.42, lon: -3.70, capital: true});
    assert.deepEqual(weatherPlace(['Valencia', 39.47, -0.38], 'ES'), {name: 'Valencia', lat: 39.47, lon: -0.38, capital: false});
    assert.equal(weatherPlace(['', 0, 0], 'XX'), null);
    assert.equal(weatherPlace(['Nowhere', 120, 0], null), null, 'junk coordinates are ignored');
    assert.equal(weatherPlace(['Nowhere', NaN, 0], 'UA').name, 'Київ');
    for (const [code, [name, lat, lon]] of Object.entries(CAPITALS)) {
        assert.match(code, /^[A-Z]{2}$/);
        assert.ok(name && Math.abs(lat) <= 90 && Math.abs(lon) <= 180, code);
    }
});

test('the forecast request carries only the place, and the answer is read safely', () => {
    const url = forecastUrl({lat: 40.42, lon: -3.7});
    assert.match(url, /^https:\/\/api\.open-meteo\.com\/v1\/forecast\?latitude=40\.42&longitude=-3\.70&/);
    const answer = JSON.stringify({current: {temperature_2m: 17.6, weather_code: 2, is_day: 0},
        daily: {temperature_2m_max: [21.2], temperature_2m_min: [10.6]}});
    assert.deepEqual(parseForecast(answer), {temp: 18, high: 21, low: 11, code: 2, day: false});
    assert.equal(parseForecast('not json'), null);
    assert.equal(parseForecast('{"current":{"temperature_2m":"hot"}}'), null);
    assert.deepEqual(parseForecast('{"current":{"temperature_2m":3,"weather_code":71}}'),
        {temp: 3, high: null, low: null, code: 71, day: true});
});

test('weather codes map to eight kinds with night icons', () => {
    assert.deepEqual(weatherKind(0, true), {kind: 'clear', index: 0, icon: 'weather-clear-symbolic'});
    assert.equal(weatherKind(0, false).icon, 'weather-clear-night-symbolic');
    assert.equal(weatherKind(63).kind, 'rain');
    assert.equal(weatherKind(81).kind, 'rain');
    assert.equal(weatherKind(75).kind, 'snow');
    assert.equal(weatherKind(96).kind, 'storm');
    assert.equal(weatherKind(48).kind, 'fog');
    assert.equal(weatherKind(1234).kind, 'overcast');
});

test('the town search sends only the text, and my location is rounded to about 10 km', () => {
    const url = geocodeUrl(' Vale ncia&x=1 ', 'uk');
    assert.equal(url, 'https://geocoding-api.open-meteo.com/v1/search?name=Vale%20ncia%26x%3D1&count=8&language=uk&format=json');
    assert.match(geocodeUrl('x', '../evil'), /language=en/);
    const found = parseGeocode(JSON.stringify({results: [
        {name: 'València', latitude: 39.47, longitude: -0.38, admin1: 'Comunitat Valenciana', country: 'Іспанія'},
        {name: 'Broken'},
    ]}));
    assert.deepEqual(found, [{name: 'València', detail: 'Comunitat Valenciana, Іспанія', lat: 39.47, lon: -0.38}]);
    assert.deepEqual(parseGeocode('{}'), []);
    assert.deepEqual(parseGeocode('<html>'), []);
    assert.equal(roundForPrivacy(39.4699), 39.5);
    assert.equal(roundForPrivacy(-0.3763), -0.4);
    assert.equal(roundForPrivacy(NaN), null);
});

test('Копії: the last backup run from the log', () => {
    const log = [
        JSON.stringify({time: 100, kind: 'backup', ok: true}),
        JSON.stringify({time: 200, kind: 'check', ok: true}),
        'broken line',
        JSON.stringify({time: 300, kind: 'backup', ok: false}),
        JSON.stringify({time: 400, kind: 'check', ok: false}),
    ].join('\n');
    assert.deepEqual(lastBackup(log), {time: 300, ok: false});
    assert.equal(lastBackup(''), null);
    assert.equal(lastBackup(null), null);
    const clock = s => `c${s}`;
    const date = s => `d${s}`;
    const day = 86400;
    assert.deepEqual(backupWhen(10 * day + 100, 10 * day + 5000, 0, clock, date), ['today', `c${10 * day + 100}`]);
    assert.deepEqual(backupWhen(9 * day + 100, 10 * day + 5000, 0, clock, date), ['yesterday', `c${9 * day + 100}`]);
    assert.deepEqual(backupWhen(5 * day, 10 * day, 0, clock, date), ['on', `d${5 * day}`]);
    // 23:30 UTC is already tomorrow at UTC+1.
    assert.equal(backupWhen(10 * day - 1800, 10 * day + 100, 3600, clock, date)[0], 'today');
});

test('a question goes to Gloxi with --ask, field codes dropped', () => {
    const flatpak = ['/usr/bin/flatpak', 'run', '--command=gloxi-gtk', 'io.github.stdenis1980.Gloxi', '%U'];
    assert.deepEqual(askArgv(flatpak, ' Що сьогодні? '),
        ['/usr/bin/flatpak', 'run', '--command=gloxi-gtk', 'io.github.stdenis1980.Gloxi', '--ask', 'Що сьогодні?']);
    assert.equal(askArgv(flatpak, '  '), null);
    assert.equal(askArgv([], 'x'), null);
    assert.deepEqual(askArgv(['gloxi-gtk'], '--help'), ['gloxi-gtk', '--ask', '--help'], 'text stays one argument');
});

test('today: events still ahead, all-day first, at most three', () => {
    const now = new Date(2026, 10, 18, 10, 0);
    const at = (h, m = 0) => new Date(2026, 10, 18, h, m);
    const events = [
        {date: at(8), end: at(9), summary: 'Over'},
        {date: at(16), end: at(17), summary: 'Build'},
        {date: at(11), end: at(11, 45), summary: ' Review '},
        {date: new Date(2026, 10, 18), end: new Date(2026, 10, 19), summary: 'Holiday'},
        {date: at(18), end: at(19), summary: ''},
        {summary: 'junk'},
    ];
    const got = todayEvents(events, now);
    assert.deepEqual(got.map(e => [e.summary, e.allDay]), [['Holiday', true], ['Review', false], ['Build', false]]);
    assert.equal(todayEvents(events, now, 10).at(-1).summary, '…');
    assert.deepEqual(todayEvents(null, now), []);
});

test('words in all ten languages, English as the fallback', () => {
    assert.deepEqual(LANGUAGES.sort(), ['cs', 'de', 'en', 'es', 'fr', 'it', 'lt', 'pl', 'ro', 'uk']);
    assert.deepEqual(checkTables(), []);
    for (const code of LANGUAGES) {
        const t = widgetText([code]);
        assert.equal(t.chips.length, 3, code);
        assert.equal(t.kinds.length, 8, code);
        assert.ok(t.capital.includes('%s') && t.last.today.includes('%s') && t.last.on.includes('%s'), code);
        assert.ok(t.dataBy.includes('Open-Meteo'), code);
    }
    assert.equal(widgetText(['uk_UA.UTF-8', 'uk', 'C']).help, 'Чим допомогти?');
    assert.equal(widgetText(['ja_JP', 'C']).help, 'How can I help?');
});

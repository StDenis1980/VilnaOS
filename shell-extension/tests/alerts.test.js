// SPDX-License-Identifier: GPL-3.0-or-later
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

import {
    ALERT_LANGUAGES, COUNTRY_FEEDS, DISCLAIMER, HAZARDS, MAX_FEED_BYTES, REFRESH_MINUTES, alertLabel, alertText,
    alertsFor, checkAlertTables, feedUrl, inRing, latestGeocodes, parseCap, parseFeed, parsePolygon, toNotify, translationTarget,
    zonesAt,
} from '../vilna@stdenis1980.github.io/lib/alerts.js';

const FEED = fs.readFileSync(new URL('./shell/alerts.atom', import.meta.url), 'utf8');
const CAP = fs.readFileSync(new URL('./shell/alert-cap.xml', import.meta.url), 'utf8');
const GEO = JSON.parse(fs.readFileSync(new URL('./shell/geocodes.json', import.meta.url), 'utf8'));
const NOW = Date.parse('2026-11-18T12:00:00Z');
const MADRID = {lat: 40.42, lon: -3.70};

test('one feed per country, fetched often enough for the terms', () => {
    assert.equal(feedUrl('ES'), 'https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-spain');
    assert.equal(feedUrl('UA'), 'https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-ukraine');
    assert.equal(feedUrl('US'), null);
    assert.equal(feedUrl(undefined), null);
    for (const [code, slug] of Object.entries(COUNTRY_FEEDS))
        assert.ok(/^[A-Z]{2}$/.test(code) && /^[a-z-]+$/.test(slug), code);
    // A delay under 5 minutes on average and never over 10.
    assert.ok(REFRESH_MINUTES <= 10);
    assert.match(DISCLAIMER, /www\.meteoalarm\.org/);
});

test('the feed is read safely: level from the colour, cancelled and junk entries skipped', () => {
    const warnings = parseFeed(FEED);
    assert.deepEqual(warnings.map(w => w.id),
        ['test.orange.wind', 'test.yellow.rain.later', 'test.red.elsewhere', 'test.yellow.fog.polygon', 'test.over']);
    const wind = warnings[0];
    assert.deepEqual([wind.colour, wind.level, wind.hazard, wind.zones, wind.area],
        ['orange', 3, 'wind', ['ES219'], 'Zona de prueba']);
    assert.equal(wind.zoneUrl, 'https://meteoalarm.org?geocode=EMMA_ID:ES219');
    assert.match(wind.capUrl, /^https:\/\/feeds\.meteoalarm\.org\//);
    assert.equal(warnings[3].polygons[0].length, 5);
    assert.deepEqual(parseFeed(''), []);
    assert.deepEqual(parseFeed('<entry><title>Orange Wind Warning</title>'), [], 'no zone, no expiry');
    assert.deepEqual(parseFeed('x'.repeat(MAX_FEED_BYTES + 1)), []);
    assert.equal(parseFeed(FEED.replace('Orange Wind', 'Purple Wind')).length, 4);
    assert.equal(parseFeed(FEED.replace('Orange Wind', 'Orange Meteorite'))[0].hazard, 'other');
});

test('polygons are checked, and malformed ones dropped', () => {
    assert.equal(parsePolygon('1,1 2,2'), null, 'fewer than three points');
    assert.equal(parsePolygon('1,1 2,x 3,3'), null);
    assert.equal(parsePolygon('91,1 2,2 3,3'), null);
    const square = parsePolygon('0,0 0,1 1,1 1,0');
    assert.equal(inRing(0.5, 0.5, square), true);
    assert.equal(inRing(1.5, 0.5, square), false);
});

test('the place gets the warnings of its zone and around it, the worst first', () => {
    const warnings = parseFeed(FEED);
    const mine = alertsFor(warnings, MADRID, ['ES219'], NOW);
    assert.deepEqual(mine.map(a => a.id), ['test.orange.wind', 'test.yellow.fog.polygon'],
        'not the one elsewhere, not the one over, not the one starting in 2099');
    assert.deepEqual(alertsFor(warnings, MADRID, [], NOW).map(a => a.id), ['test.yellow.fog.polygon'],
        'without zones only the polygons match');
    assert.deepEqual(alertsFor(warnings, {lat: 50, lon: 30}, [], NOW), []);
    assert.deepEqual(alertsFor(warnings, null, ['ES219'], NOW), []);
    assert.equal(alertsFor([...warnings, {...warnings[0], id: 'twice'}], MADRID, ['ES219'], NOW).length, 2,
        'the same warning twice is shown once');
});

test('orange and red warnings bring a notification, each once', () => {
    const mine = alertsFor(parseFeed(FEED), MADRID, ['ES219'], NOW);
    assert.deepEqual(toNotify(mine, []).map(a => a.id), ['test.orange.wind'], 'not the yellow one');
    assert.deepEqual(toNotify(mine, ['test.orange.wind']), []);
    assert.deepEqual(toNotify(null, null), []);
});

test('the original words come unchanged, in the person’s language when written', () => {
    assert.deepEqual(parseCap(CAP, ['es_ES.UTF-8', 'es']),
        {language: 'es-ES', headline: 'Aviso naranja de prueba. Zona de prueba', description: 'Rachas de prueba.',
            sender: 'Servicio de prueba', web: ''});
    assert.equal(parseCap(CAP, ['uk_UA.UTF-8', 'uk']).language, 'en-GB', 'else English');
    assert.equal(parseCap('<alert/>', ['en']), null);
    assert.equal(parseCap(null, null), null);
});

test('the zones of a place are found in the boundaries file, holes and junk included', () => {
    assert.deepEqual(zonesAt(GEO, 40.42, -3.70), ['ES219']);
    assert.deepEqual(zonesAt(GEO, 40.65, -3.5), [], 'inside the hole');
    assert.deepEqual(zonesAt(GEO, 39.2, 0.5), ['ES001'], 'a multipolygon');
    assert.deepEqual(zonesAt(GEO, NaN, 0), []);
    assert.deepEqual(zonesAt(null, 40, -3), []);
    const tree = JSON.stringify([{name: 'README.md'}, {name: 'MeteoAlarm_Geocodes_2026_02_20.json'},
        {name: 'MeteoAlarm_Geocodes_2026_11_13.json'}, {name: 'MeteoAlarm_Geocodes-Aliases_2026_11_13.csv'}]);
    assert.deepEqual(latestGeocodes(tree), {name: 'MeteoAlarm_Geocodes_2026_11_13.json',
        url: 'https://gitlab.com/meteoalarm-pm-group/documents/-/raw/master/MeteoAlarm_Geocodes_2026_11_13.json'});
    assert.equal(latestGeocodes('[]'), null);
    assert.equal(latestGeocodes('nope'), null);
});

test('labels in all ten languages', () => {
    assert.deepEqual(ALERT_LANGUAGES.sort(), ['cs', 'de', 'en', 'es', 'fr', 'it', 'lt', 'pl', 'ro', 'uk']);
    assert.deepEqual(checkAlertTables(), []);
    for (const code of ALERT_LANGUAGES) {
        const t = alertText([code]);
        for (const key of ['until', 'from', 'zone'])
            assert.ok(t[key].includes('%s'), `${code} ${key}`);
        assert.equal(t.issued.split('%s').length, 3, code);
        assert.match(t.source, /EUMETNET – MeteoAlarm/, code);
    }
    const uk = alertText(['uk_UA.UTF-8']);
    assert.equal(alertLabel({level: 3, hazard: 'wind'}, uk), 'Помаранчеве попередження: Вітер');
    assert.equal(alertLabel({level: 4, hazard: 'nonsense'}, uk), 'Червоне попередження: Погода');
    assert.equal(alertText(['ja']).title, 'Weather warnings');
    assert.equal(HAZARDS.length + 1, uk.hazards.length);
});

test('Gloxi translates only words that are not in the person’s language', () => {
    const es = {headline: 'Aviso naranja por viento', language: 'es-ES'};
    assert.equal(translationTarget(es, ['uk_UA.UTF-8', 'uk_UA', 'uk', 'C']), 'uk');
    assert.equal(translationTarget(es, ['es_ES.UTF-8', 'es', 'C']), null);
    assert.equal(translationTarget({headline: 'Orange warning', language: 'en-GB'}, ['en_GB.UTF-8', 'C']), null);
    // A language Vilna does not speak: our labels are English, so is the translation.
    assert.equal(translationTarget(es, ['ja_JP.UTF-8', 'C']), 'en');
    assert.equal(translationTarget({headline: '', language: 'es'}, ['uk']), null);
    assert.equal(translationTarget(null, ['uk']), null);
    for (const code of ALERT_LANGUAGES)
        assert.match(alertText([code]).translated, /Gloxi/);
});

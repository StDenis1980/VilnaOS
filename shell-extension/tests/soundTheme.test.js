// SPDX-License-Identifier: GPL-3.0-or-later
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

import {
    SOUND_EVENTS, SOUND_THEMES, baseTheme, disabledFiles, indexTheme, plan,
} from '../vilna@stdenis1980.github.io/lib/soundTheme.js';

test('the user theme inherits the chosen Vilna theme', () => {
    assert.match(indexTheme('bell'), /^Inherits=vilna-bell$/m);
    assert.match(indexTheme('freedesktop'), /^Inherits=freedesktop$/m);
    // Junk in the setting falls back to a real theme, never to a path.
    assert.equal(baseTheme('../../etc'), 'vilna-wood');
    assert.match(indexTheme('wood'), /^\[Sound Theme\]$/m);
});

test('only known events become .disabled files', () => {
    assert.deepEqual(disabledFiles(['device-added', '../x', 'bell', 'bell']),
        ['bell.disabled', 'device-added.disabled']);
    assert.deepEqual(disabledFiles(undefined), []);
});

test('the plan writes only what changed', () => {
    const index = indexTheme('wood');
    assert.deepEqual(plan('wood', ['bell'], index, ['index.theme', 'bell.disabled']),
        {index: null, create: [], remove: []});
    assert.deepEqual(plan('bell', ['complete'], index, ['index.theme', 'bell.disabled', 'notes.txt']),
        {index: indexTheme('bell'), create: ['complete.disabled'], remove: ['bell.disabled']});
});

test('the schema, the sound themes and the generator agree', () => {
    const schema = fs.readFileSync(new URL('../vilna@stdenis1980.github.io/schemas/org.gnome.shell.extensions.vilna.gschema.xml', import.meta.url), 'utf8');
    const choices = [...schema.matchAll(/<choice value="([a-z]+)"\/>/g)].map(m => m[1]);
    for (const theme of SOUND_THEMES)
        assert.ok(choices.includes(theme), theme);
    const maker = fs.readFileSync(new URL('../../obs/packages/vilna-sound-themes/make_sounds.py', import.meta.url), 'utf8');
    const motifs = maker.slice(maker.indexOf('MOTIFS'), maker.indexOf('# Theme names'));
    const events = [...motifs.matchAll(/^ {4}"([a-z0-9-]+)": \(/gm)].map(m => m[1]);
    assert.deepEqual(events, SOUND_EVENTS);
});

test('the Sounds page has every word in the ten languages', async () => {
    const {SOUND_LANGUAGES, checkSoundTables, soundText} = await import('../vilna@stdenis1980.github.io/lib/soundTheme.js');
    assert.deepEqual(checkSoundTables(), []);
    assert.deepEqual(SOUND_LANGUAGES, ['en', 'uk', 'es', 'pl', 'de', 'fr', 'it', 'ro', 'cs', 'lt']);
    const uk = soundText(['uk_UA.UTF-8', 'uk', 'C']);
    assert.equal(uk.page, 'Звуки');
    assert.equal(uk.themes.star, 'Зірка');
    assert.equal(uk.events['device-added'], 'Пристрій під’єднано');
    assert.equal(soundText(['xx']).page, 'Sounds');
});

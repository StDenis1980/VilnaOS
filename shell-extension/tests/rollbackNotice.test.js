// SPDX-License-Identifier: GPL-3.0-or-later
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {LANGUAGES, MAX_AGE_SECONDS, noticeText, parseNote, shouldShow} from '../vilna@stdenis1980.github.io/lib/rollbackNotice.js';

// As vilna-health-last-working writes it.
const NOTE = 'failed=4\nrestored=1\ntime=1763640000\n';

test('reads the note of vilna-health-last-working', () => {
    assert.deepEqual(parseNote(NOTE), {failed: '4', restored: '1', time: 1763640000});
    // systemd-boot picked the entry itself.
    assert.equal(parseNote('failed=4\nrestored=\ntime=1763640000\n').restored, null);
});

test('junk is not a note', () => {
    for (const junk of [null, undefined, '', 'hello', 'failed=4\n', 'time=1\n', 'failed=x\ntime=1\n', 'x'.repeat(5000) + NOTE])
        assert.equal(parseNote(junk), null, String(junk).slice(0, 20));
});

test('shown once per failed snapshot', () => {
    const note = parseNote(NOTE);
    const now = note.time + 60;
    assert.equal(shouldShow(note, '', now), true);
    assert.equal(shouldShow(note, '4', now), false);
    // A later failure of another snapshot is news again.
    assert.equal(shouldShow(parseNote('failed=7\nrestored=5\ntime=1763640000\n'), '4', now), true);
    assert.equal(shouldShow(null, '', now), false);
});

test('an old note is not news, a clock behind the note still shows it', () => {
    const note = parseNote(NOTE);
    assert.equal(shouldShow(note, '', note.time + MAX_AGE_SECONDS), true);
    assert.equal(shouldShow(note, '', note.time + MAX_AGE_SECONDS + 1), false);
    assert.equal(shouldShow(note, '', note.time - 3600), true);
});

test('the words in all main languages, English otherwise', () => {
    assert.equal(LANGUAGES.length, 15);
    for (const lang of LANGUAGES) {
        const t = noticeText([lang]);
        assert.ok(t.title && t.body, lang);
    }
    assert.equal(noticeText(['uk_UA.UTF-8', 'uk', 'C']).title, 'Оновлення скасовано');
    assert.equal(noticeText(['ja_JP.UTF-8', 'C']).title, 'Update undone');
});

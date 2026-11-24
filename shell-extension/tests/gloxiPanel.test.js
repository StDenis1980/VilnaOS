// SPDX-License-Identifier: GPL-3.0-or-later
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {
    MAX_MESSAGES, PanelChat, checkTables, helloLanguage, owlFile, panelText, safeUri,
} from '../vilna@stdenis1980.github.io/lib/gloxiPanel.js';

const feed = (chat, ...events) => events.forEach(e => chat.apply(e));

test('an answer streams into one Gloxi line and the owl follows it', () => {
    const chat = new PanelChat();
    feed(chat, {event: 'busy', busy: true}, {event: 'user_message', text: 'привіт'},
        {event: 'start_answer', label: 'On this computer'});
    assert.equal(chat.state, 'thinking');
    feed(chat, {event: 'append_answer', piece: 'При'}, {event: 'append_answer', piece: 'віт'});
    assert.equal(chat.state, 'answering');
    feed(chat, {event: 'finish_answer'}, {event: 'busy', busy: false});
    assert.equal(chat.state, 'idle');
    assert.deepEqual(chat.lines.map(l => [l.kind, l.text]), [['user', 'привіт'], ['gloxi', 'Привіт']]);
});

test('an answer without words leaves no empty line', () => {
    const chat = new PanelChat();
    feed(chat, {event: 'start_answer', label: 'x'}, {event: 'finish_answer'});
    assert.equal(chat.lines.length, 0);
});

test('an error shows until the person writes again', () => {
    const chat = new PanelChat();
    feed(chat, {event: 'error', text: 'model gone'});
    assert.equal(chat.state, 'error');
    feed(chat, {event: 'user_message', text: 'ще раз'});
    assert.notEqual(chat.state, 'error');
});

test('history comes back, only what was said, and the panel keeps a bounded tail', () => {
    const chat = new PanelChat();
    const history = Array.from({length: MAX_MESSAGES + 50}, (_, i) => ({role: i % 2 ? 'assistant' : 'user', text: `m${i}`}));
    chat.loadHistory([{role: 'tool', text: 'secret'}, ...history]);
    assert.ok(chat.lines.length <= MAX_MESSAGES);
    assert.equal(chat.lines.at(-1).text, `m${MAX_MESSAGES + 49}`);
    assert.ok(!chat.lines.some(l => l.text === 'secret'));
    for (let i = 0; i < 10; i++)
        chat.apply({event: 'note', text: `n${i}`});
    assert.ok(chat.lines.length <= MAX_MESSAGES);
});

test('junk from the socket changes nothing and does not throw', () => {
    const chat = new PanelChat();
    for (const junk of [null, 5, 'x', [], {}, {event: 7}, {event: 'nothing'}, {event: 'append_answer'},
        {event: 'action', card: 'not a card'}, {event: 'user_message', text: {}}])
        chat.apply(junk);
    assert.ok(chat.lines.every(l => typeof l.text === 'string'));
    const card = chat.lines.find(l => l.kind === 'action');
    assert.equal(card?.status, 'error');
});

test('a card says what was done and whether Keros found something', () => {
    const chat = new PanelChat();
    chat.apply({event: 'action', card: {status: 'done', program: 'Files', operation: 'move', warnings: ['x']}});
    assert.deepEqual(chat.lines[0], {kind: 'action', text: 'Files: move', status: 'done', warned: true});
});

test('the owl picture follows the state and the style', () => {
    assert.notEqual(owlFile('thinking', false), owlFile('idle', false));
    assert.notEqual(owlFile('idle', true), owlFile('idle', false));
    assert.equal(owlFile('anything', false), owlFile('idle', false));
});

test('only https and mailto links open, as in the window', () => {
    assert.ok(safeUri('https://vilnaos.com/'));
    assert.ok(safeUri('mailto:hello@vilnaos.com'));
    for (const bad of ['http://x', 'file:///etc/passwd', 'javascript:alert(1)', 'https://x y', 5, null])
        assert.ok(!safeUri(bad), String(bad));
});

test('hello carries a real language, never C', () => {
    assert.equal(helloLanguage(['uk_UA.UTF-8', 'uk', 'C']), 'uk_UA.UTF-8');
    assert.equal(helloLanguage(['C']), null);
    assert.equal(helloLanguage(['uk; rm -rf /', 'pl']), 'pl');
});

test('every language has every word, and an unknown one gets English', () => {
    assert.deepEqual(checkTables(), []);
    assert.equal(panelText(['xx_XX', 'C']).send, panelText(['en']).send);
    assert.notEqual(panelText(['uk_UA.UTF-8']).send, panelText(['en']).send);
});

// SPDX-License-Identifier: GPL-3.0-or-later
//
// Stage B of the Gloxi window: the side panel the Shell draws in the strip
// dock.js reserves. It is a client of gloxid, the Gloxi service, like the
// Gloxi window (gloxi/docs/service-protocol.md, hello with client=panel):
// the same conversation, its history and the owl's state; what the person
// types here goes to the same Gloxi. Approvals stay with Herkos's own
// dialog, and settings, cards in full and the model switch with the window,
// which the panel opens with a button.
//
// lib/gloxiPanel.js has the logic and the words; this file only draws and
// talks to the socket.

import Atk from 'gi://Atk';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import Pango from 'gi://Pango';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {
    MAX_INPUT, PanelChat, helloLanguage, owlFile, panelText, requestLine, safeUri, stateText,
} from './lib/gloxiPanel.js';
import {verticalBox} from './vbox.js';

const GLOXI_DESKTOP = 'io.github.stdenis1980.Gloxi.desktop';
const CALL_SECONDS = 15;

/**
 * Whether the keyboard focus is in this actor. With the focus in a window
 * the stage has none, and contains(null) throws in GNOME Shell 51.
 */
export function hasKeyFocus(actor) {
    const focus = global.stage.get_key_focus();
    return focus !== null && actor.contains(focus);
}

// Where gloxid listens (gloxi/docs/service-protocol.md).
export function gloxiSocket() {
    return GLib.getenv('GLOXI_SERVICE_SOCKET') ||
        GLib.build_filenamev(['/run/gloxi', String(new Gio.Credentials().get_unix_user()), 'gloxi.sock']);
}

function promise(start) {
    return new Promise((resolve, reject) => start((source, result, finish) => {
        try {
            resolve(finish(result));
        } catch (e) {
            reject(e);
        }
    }));
}

/** One connection to gloxid: requests with answers, and its events. */
class GloxiClient {
    constructor(onLine, onClosed) {
        this._onLine = onLine;
        this._onClosed = onClosed;
        this._conn = null;
        this._cancellable = null;
        this._pending = new Map();
        this._nextId = 1;
        this._writing = Promise.resolve();
    }

    get connected() {
        return this._conn !== null;
    }

    async open() {
        this._cancellable = new Gio.Cancellable();
        const client = new Gio.SocketClient();
        const address = Gio.UnixSocketAddress.new(gloxiSocket());
        this._conn = await promise(done => client.connect_async(address, this._cancellable,
            (c, r) => done(c, r, res => c.connect_finish(res))));
        this._input = new Gio.DataInputStream({base_stream: this._conn.get_input_stream()});
        this._read();
    }

    _read() {
        const cancellable = this._cancellable;
        this._input.read_line_async(GLib.PRIORITY_DEFAULT, cancellable, (input, result) => {
            let line = null;
            try {
                [line] = input.read_line_finish_utf8(result);
            } catch {
                line = null;
            }
            if (cancellable.is_cancelled())
                return;
            if (line === null) {
                this.close();
                this._onClosed();
                return;
            }
            let message = null;
            try {
                message = JSON.parse(line);
            } catch {
                message = null;
            }
            // A fault while showing one line must not stop the reading:
            // the rest of the answer and every later event would be lost.
            try {
                if (message && typeof message === 'object' && 'id' in message && !('event' in message)) {
                    const waiting = this._pending.get(message.id);
                    this._pending.delete(message.id);
                    waiting?.(message);
                } else if (message) {
                    this._onLine(message);
                }
            } catch (e) {
                console.error(`Vilna Gloxi panel: ${e.message}\n${e.stack}`);
            }
            this._read();
        });
    }

    /** The result of one request, or an Error. */
    call(method, params = {}) {
        if (!this._conn)
            return Promise.reject(new Error('not connected'));
        const id = this._nextId++;
        const bytes = new TextEncoder().encode(requestLine(id, method, params));
        const answer = new Promise((resolve, reject) => {
            const timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, CALL_SECONDS, () => {
                this._pending.delete(id);
                reject(new Error(`no answer to ${method}`));
                return GLib.SOURCE_REMOVE;
            });
            this._pending.set(id, message => {
                GLib.source_remove(timer);
                if (message.error)
                    reject(new Error(`${message.error.code}: ${message.error.message}`));
                else
                    resolve(message.result ?? {});
            });
        });
        const output = this._conn.get_output_stream();
        this._writing = this._writing.then(() => this._write(output, bytes)).catch(e => {
            this._pending.get(id)?.({error: {code: 'io', message: e.message}});
        });
        return answer;
    }

    async _write(output, bytes) {
        let rest = bytes;
        while (rest.length > 0) {
            const n = await promise(done => output.write_bytes_async(new GLib.Bytes(rest), GLib.PRIORITY_DEFAULT,
                this._cancellable, (o, r) => done(o, r, res => o.write_bytes_finish(res))));
            if (n <= 0)
                throw new Error('gloxid takes nothing');
            rest = rest.subarray(n);
        }
    }

    close() {
        this._cancellable?.cancel();
        try {
            this._conn?.close(null);
        } catch {
            // Already closed.
        }
        this._conn = null;
        for (const waiting of this._pending.values())
            waiting({error: {code: 'closed', message: 'gloxid closed the connection'}});
        this._pending.clear();
    }
}

function iconButton(iconName, name, onClick) {
    const button = new St.Button({
        style_class: 'vilna-gloxi-button',
        can_focus: true,
        accessible_name: name,
        child: new St.Icon({icon_name: iconName, icon_size: 16}),
    });
    button.connect('clicked', onClick);
    return button;
}

/**
 * What the panel shows inside the dock's strip. The strip owns the actor
 * and its place; this fills it and keeps it in step with gloxid.
 */
export class PanelView {
    // dir: the extension's directory (owl/); onClose: close the panel.
    constructor(dir, onClose) {
        this._dir = dir;
        this._onClose = onClose;
        this._t = panelText(GLib.get_language_names());
        this._chat = new PanelChat();
        this._client = new GloxiClient(m => this._onLine(m), () => this._onLost());
        this._actors = new Map(); // a line of PanelChat → its actor
        this._connecting = null;
        this._interface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
        this._schemeId = this._interface.connect('changed::color-scheme', () => this._showState());
        this.actor = this._build();
        this._showState();
        this._connect();
    }

    destroy() {
        this._interface.disconnect(this._schemeId);
        this._client.close();
        this.actor.destroy();
    }

    // For the headless test.
    get chat() {
        return this._chat;
    }

    get entry() {
        return this._entry;
    }

    _build() {
        const t = this._t;
        const box = verticalBox({style_class: 'vilna-gloxi-panel', x_expand: true, y_expand: true});
        box.name = 'vilnaGloxiPanel';
        box.accessible_name = t.indicator;

        const header = new St.BoxLayout({style_class: 'vilna-gloxi-header'});
        this._owl = new St.Icon({icon_size: 40, style_class: 'vilna-gloxi-owl'});
        header.add_child(this._owl);
        const titles = verticalBox({x_expand: true, y_align: Clutter.ActorAlign.CENTER});
        titles.add_child(new St.Label({text: 'Gloxi', style_class: 'vilna-gloxi-title'}));
        this._stateLabel = new St.Label({style_class: 'vilna-gloxi-state'});
        titles.add_child(this._stateLabel);
        header.add_child(titles);
        header.add_child(iconButton('view-fullscreen-symbolic', t.openWindow, () => this._openWindow()));
        header.add_child(iconButton('window-close-symbolic', t.close, () => this._onClose()));
        box.add_child(header);

        this._scroll = new St.ScrollView({
            style_class: 'vilna-gloxi-scroll', x_expand: true, y_expand: true,
            hscrollbar_policy: St.PolicyType.NEVER, vscrollbar_policy: St.PolicyType.AUTOMATIC,
        });
        this._list = verticalBox({style_class: 'vilna-gloxi-messages', x_expand: true});
        this._list.accessible_name = t.messages;
        if (typeof this._scroll.set_child === 'function')
            this._scroll.set_child(this._list);
        else
            this._scroll.add_actor(this._list);
        box.add_child(this._scroll);
        // Before the first message too, the person sees that Gloxi is an AI (ADR 0033).
        this._empty = new St.Label({
            text: `${t.empty}\n\n${t.aiNotice}`, style_class: 'vilna-gloxi-empty', x_align: Clutter.ActorAlign.CENTER,
        });
        this._empty.clutter_text.line_wrap = true;
        this._empty.clutter_text.line_alignment = Pango.Alignment.CENTER;
        this._list.add_child(this._empty);

        const input = new St.BoxLayout({style_class: 'vilna-gloxi-input'});
        // The field grows with the text, line by line, up to a few lines
        // (max-height in the stylesheet), then scrolls (Build12.29: one
        // short line, long text out of sight). Enter sends, Shift+Enter
        // starts a new line.
        this._entry = new St.Entry({
            hint_text: t.hint, can_focus: true, x_expand: true, style_class: 'vilna-gloxi-entry',
        });
        this._entry.accessible_name = t.hint.replace(/…$/, '');
        const text = this._entry.clutter_text;
        text.max_length = MAX_INPUT;
        text.single_line_mode = false;
        text.activatable = false;
        text.line_wrap = true;
        text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        text.connect('key-press-event', (_a, event) => {
            const key = event.get_key_symbol();
            if (key === Clutter.KEY_Escape) {
                this._onClose();
                return Clutter.EVENT_STOP;
            }
            if ((key === Clutter.KEY_Return || key === Clutter.KEY_KP_Enter) &&
                !(event.get_state() & Clutter.ModifierType.SHIFT_MASK)) {
                this._submit();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
        this._field = new St.ScrollView({
            style_class: 'vilna-gloxi-field', x_expand: true, y_align: Clutter.ActorAlign.END,
            hscrollbar_policy: St.PolicyType.NEVER, vscrollbar_policy: St.PolicyType.AUTOMATIC,
        });
        // A scroll view takes only a scrollable child: a box round the entry.
        const fieldBox = verticalBox({x_expand: true});
        fieldBox.add_child(this._entry);
        if (typeof this._field.set_child === 'function')
            this._field.set_child(fieldBox);
        else
            this._field.add_actor(fieldBox);
        // The border goes round the scrolled field, so it shows focus.
        this._entry.connect('key-focus-in', () => this._field.add_style_pseudo_class('focus'));
        this._entry.connect('key-focus-out', () => this._field.remove_style_pseudo_class('focus'));
        text.connect('cursor-changed', () => this._showCursor());
        input.add_child(this._field);
        this._send = iconButton('mail-send-symbolic', t.send, () => this._submit());
        input.add_child(this._send);
        box.add_child(input);
        // Tab and the arrows move between the panel's controls.
        global.focus_manager.add_group(box);
        return box;
    }

    focus() {
        this._entry.grab_key_focus();
    }

    // Keeps the line with the text cursor in view once the field scrolls.
    _showCursor() {
        const adjustment = this._field.vadjustment ?? this._field.vscroll?.adjustment;
        const text = this._entry.clutter_text;
        if (!adjustment)
            return;
        const [ok, , y, lineHeight] = text.position_to_coords(text.cursor_position);
        if (!ok)
            return;
        // In the field's scrolled coordinates: the text sits inside the
        // entry's padding.
        const lineTop = y + text.y;
        if (lineTop < adjustment.value)
            adjustment.value = lineTop;
        else if (lineTop + lineHeight > adjustment.value + adjustment.page_size)
            adjustment.value = lineTop + lineHeight - adjustment.page_size;
    }

    /** The field's height in pixels; for the headless test. */
    get fieldHeight() {
        return this._field.height;
    }

    async _connect() {
        if (this._client.connected)
            return true;
        this._connecting ??= (async () => {
            try {
                await this._client.open();
                const params = {client: 'panel'};
                const language = helloLanguage(GLib.get_language_names());
                if (language)
                    params.language = language;
                const hello = await this._client.call('hello', params);
                this._chat.loadHistory(hello.history, this._t.aiNotice);
                this._chat.busy = Boolean(hello.state?.busy);
                this._chat.error = false;
                this._render();
                return true;
            } catch (e) {
                console.log(`Vilna Gloxi panel: no gloxid: ${e.message}`);
                this._client.close();
                this._onLost();
                return false;
            } finally {
                this._connecting = null;
            }
        })();
        return this._connecting;
    }

    /** A Gloxi notification, shown here instead of as a banner. */
    notice(title, body) {
        if (this._chat.notice(title, body))
            this._render();
    }

    _onLost() {
        this._chat.offline = true;
        this._chat.busy = false;
        this._render();
    }

    _onLine(message) {
        if (message.event === 'replaced') {
            // Another panel (another login of the same person) took over.
            this._chat.apply({event: 'note', text: this._t.replaced});
            this._client.close();
            this._onLost();
            return;
        }
        if (message.event === 'open_uri') {
            if (safeUri(message.url)) {
                try {
                    Gio.app_info_launch_default_for_uri(message.url, global.create_app_launch_context(0, -1));
                } catch (e) {
                    console.warn(`Vilna Gloxi panel: cannot open a link: ${e.message}`);
                }
            }
            return;
        }
        if (this._chat.apply(message))
            this._render();
    }

    async _submit() {
        const text = this._entry.get_text().trim();
        if (!text || this._chat.busy)
            return;
        if (!await this._connect())
            return;
        this._chat.asking();
        try {
            const result = await this._client.call('chat.submit', {text});
            if (result.accepted)
                this._entry.set_text('');
            else
                this._chat.asking(false);
        } catch (e) {
            this._chat.asking(false);
            this._chat.apply({event: 'error', text: e.message});
            this._render();
        }
    }

    _openWindow() {
        const app = Shell.AppSystem.get_default().lookup_app(GLOXI_DESKTOP);
        if (app)
            app.activate();
        else
            console.warn('Vilna Gloxi panel: the Gloxi app is not installed');
    }

    _showState() {
        const state = this._chat.state;
        const dark = this._interface.get_string('color-scheme') === 'prefer-dark';
        const file = this._dir.get_child('owl').get_child(owlFile(state, dark));
        this._owl.gicon = new Gio.FileIcon({file});
        const words = stateText(this._t, state);
        this._stateLabel.text = words;
        this._owl.accessible_name = words;
        this.actor.accessible_description = words;
        this._send.reactive = !this._chat.busy;
    }

    _lineActor(line) {
        const t = this._t;
        const box = verticalBox({style_class: `vilna-gloxi-line ${line.kind}`, x_expand: true});
        if (line.caption)
            box.add_child(new St.Label({text: line.caption, style_class: 'vilna-gloxi-caption'}));
        const label = new St.Label({style_class: 'vilna-gloxi-text', x_expand: true});
        label.clutter_text.line_wrap = true;
        label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        box.add_child(label);
        box._vilnaText = label;
        box._vilnaWho = line.kind === 'user' ? t.you : line.kind === 'gloxi' ? 'Gloxi' : '';
        if (line.kind === 'action' && line.warned)
            box.add_child(new St.Label({text: t.suspicious, style_class: 'vilna-gloxi-caption warning'}));
        return box;
    }

    _lineText(line) {
        if (line.kind !== 'action')
            return line.text || '…';
        const status = {done: this._t.done, denied: this._t.denied, error: this._t.failed}[line.status];
        return `${line.text || '…'} · ${status}`;
    }

    _render() {
        const shown = new Set(this._chat.lines);
        for (const [line, actor] of this._actors) {
            if (!shown.has(line)) {
                actor.destroy();
                this._actors.delete(line);
            }
        }
        this._chat.lines.forEach((line, i) => {
            let actor = this._actors.get(line);
            if (!actor) {
                actor = this._lineActor(line);
                this._actors.set(line, actor);
                this._list.add_child(actor);
            }
            this._list.set_child_at_index(actor, i + 1);
            const text = this._lineText(line);
            if (actor._vilnaText.text !== text)
                actor._vilnaText.text = text;
            // Who said it is part of the name a screen reader reads.
            actor.accessible_name = actor._vilnaWho ? `${actor._vilnaWho}: ${text}` : text;
        });
        this._empty.visible = this._chat.lines.length === 0;
        this._showState();
        this._scrollToEnd();
        this._speak();
    }

    // St has no live regions: new lines are handed to the screen reader as
    // an ATK notification (AT-SPI Announcement, which Orca reads), from the
    // message list. Read whole, once an answer is finished, not word by word.
    _speak() {
        const focused = hasKeyFocus(this.actor);
        const news = this._chat.takeNews(focused);
        if (!news.length)
            return;
        const accessible = this._list.get_accessible?.();
        if (!accessible || !GObject.signal_lookup('notification', accessible.constructor.$gtype))
            return;
        for (const {line, urgent} of news) {
            const actor = this._actors.get(line);
            const words = actor?.accessible_name || this._lineText(line);
            accessible.emit('notification', words, urgent ? Atk.Live.ASSERTIVE : Atk.Live.POLITE);
        }
    }

    _scrollToEnd() {
        const adjustment = this._scroll.vadjustment ?? this._scroll.vscroll?.adjustment;
        if (!adjustment)
            return;
        // After the new lines have their height.
        const laters = global.compositor?.get_laters?.();
        const go = () => {
            adjustment.value = adjustment.upper - adjustment.page_size;
            return GLib.SOURCE_REMOVE;
        };
        if (laters)
            laters.add(Meta.LaterType.BEFORE_REDRAW, go);
        else
            GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, go);
    }
}

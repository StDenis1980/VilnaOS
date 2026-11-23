// SPDX-License-Identifier: GPL-3.0-or-later
//
// (и) After an automatic rollback, one notification (lib/rollbackNotice.js).
//
// Read once when the person's session starts: the note is written before
// the reboot into the working snapshot, so it is there by the login.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';

import {NOTE_PATH, noticeText, parseNote, shouldShow} from './lib/rollbackNotice.js';

export class RollbackNotice {
    constructor(settings, path = NOTE_PATH) {
        this._settings = settings;
        this._path = path;
        this._cancellable = null;
    }

    enable() {
        this._cancellable = new Gio.Cancellable();
        const file = Gio.File.new_for_path(this._path);
        file.load_contents_async(this._cancellable, (_f, res) => {
            let text;
            try {
                const [, bytes] = file.load_contents_finish(res);
                text = new TextDecoder().decode(bytes);
            } catch (e) {
                // No note: no rollback. Anything else is worth a line.
                if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.NOT_FOUND) &&
                    !e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    console.warn(`Vilna: could not read ${this._path}: ${e.message}`);
                return;
            }
            const note = parseNote(text);
            const now = Math.floor(GLib.get_real_time() / 1e6);
            if (!shouldShow(note, this._settings.get_string('rollback-noticed'), now))
                return;
            this._settings.set_string('rollback-noticed', note.failed);
            this._notify();
        });
    }

    disable() {
        this._cancellable?.cancel();
        this._cancellable = null;
    }

    _notify() {
        const text = noticeText(GLib.get_language_names());
        const source = MessageTray.getSystemSource();
        const notification = new MessageTray.Notification({
            source,
            title: text.title,
            body: text.body,
            iconName: 'software-update-urgent-symbolic',
        });
        source.addNotification(notification);
    }
}

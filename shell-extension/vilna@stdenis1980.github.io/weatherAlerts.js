// SPDX-License-Identifier: GPL-3.0-or-later
//
// Weather warnings on the weather card (decision of Denys, 18.11.2026):
// MeteoAlarm's warnings for the weather place, as a coloured band, and a
// notification for each orange or red one. lib/alerts.js says where the
// data comes from, how it is matched without sending the place anywhere,
// and how MeteoAlarm's terms are met.
//
// Traffic: the country's feed (up to about 0.5 MB) every 10 minutes and,
// once per place, the zone boundaries (about 30 MB, in alertZones.js).
// On a metered connection nothing is downloaded until the person agrees
// (the band asks; alerts-metered in the preferences). Nothing is fetched
// while the screen is locked.
//
// Words in another language: when the service gave no text in the
// person's language, Gloxi translates it on this computer (gloxid's
// text.translate, local model only) and the translation stands above the
// unchanged original (Denys, 18.11.2026; MeteoAlarm terms). Words already
// in the person's language are shown as they are, without Gloxi. Without
// gloxid (outside Vilna) or its model, only the original is shown.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';

import {
    REFRESH_MINUTES, SITE_URL, alertLabel, alertText, alertsFor, feedUrl, parseCap, parseFeed, toNotify,
    translationTarget,
} from './lib/alerts.js';
import {verticalBox} from './vbox.js';

Gio._promisify(Gio.Subprocess.prototype, 'communicate_utf8_async');
Gio._promisify(Gio.SocketClient.prototype, 'connect_async');
Gio._promisify(Gio.OutputStream.prototype, 'write_bytes_async');
Gio._promisify(Gio.DataInputStream.prototype, 'read_line_async', 'read_line_finish_utf8');

// Shown at most; the worst first.
const MAX_SHOWN = 2;
// Remembered notified warnings, so that each comes once across restarts.
const MAX_NOTIFIED = 300;
const RETRY_MINUTES = 5;
// A translation by the local model takes seconds; a slow computer busy with
// a conversation may take longer. Past this the original stands alone.
const TRANSLATE_SECONDS = 120;
// Tests only: files read instead of the network (headless Shell test).
const TEST_FEED = GLib.getenv('VILNA_TEST_ALERTS_FILE');
const TEST_CAP = GLib.getenv('VILNA_TEST_CAP_FILE');
const TEST_GEOCODES = GLib.getenv('VILNA_TEST_GEOCODES_FILE');

function readText(path) {
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        return ok ? new TextDecoder().decode(bytes) : null;
    } catch {
        return null;
    }
}

const near = (a, b) => Math.abs(a - b) < 1e-4;

// Where gloxid listens (gloxi/docs/service-protocol.md).
function gloxiSocket() {
    return GLib.getenv('GLOXI_SERVICE_SOCKET') ||
        GLib.build_filenamev(['/run/gloxi', String(new Gio.Credentials().get_unix_user()), 'gloxi.sock']);
}

// A warning's words and their language: the service's own, or the feed's
// English title when its text could not be fetched.
function wordsOf(alert) {
    if (alert.original?.headline)
        return {text: alert.original.headline, language: alert.original.language};
    return {text: alert.event || alert.title, language: 'en'};
}

// Long warnings wrap instead of being cut: every word of the service
// stays visible (MeteoAlarm terms).
function wrap(label) {
    label.clutter_text.line_wrap = true;
    label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    return label;
}

export class WeatherAlerts {
    // dir: the extension's directory (Gio.File); place(): the weather
    // place or null; homeCountry(): the system's country.
    constructor(settings, dir, place, homeCountry) {
        this._settings = settings;
        this._dir = dir;
        this._placeOf = place;
        this._homeCountry = homeCountry;
        this._text = alertText(GLib.get_language_names());
        this._languages = GLib.get_language_names();
        this._timer = 0;
        this._caps = new Map();
        this._translations = new Map();
        this._zonesRunning = null;
        this._session = new Soup.Session({user_agent: 'Vilna desktop widgets', timeout: 30});
        this._cancellable = new Gio.Cancellable();
        this._stateFile = GLib.build_filenamev([GLib.get_user_state_dir(), 'vilna', 'alerts-notified.json']);
        this._notified = this._loadNotified();

        this.actor = verticalBox({x_expand: true, style_class: 'vilna-alerts', visible: false});
        this.actor.name = 'vilnaWeatherAlerts';

        this._monitor = Gio.NetworkMonitor.get_default();
        this._ids = [
            ...['changed::weather-alerts', 'changed::alerts-metered']
                .map(signal => [this._settings, this._settings.connect(signal, () => this.update())]),
            [this._monitor, this._monitor.connect('notify::network-metered', () => this.update())],
            [Main.sessionMode, Main.sessionMode.connect('updated', () => {
                if (!Main.sessionMode.isLocked)
                    this.update();
            })],
        ];
        this._alerts = [];
    }

    destroy() {
        for (const [object, id] of this._ids)
            object.disconnect(id);
        this._ids = [];
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        this._cancellable.cancel();
        this._zonesRunning?.force_exit();
        this._session.abort();
        this._tray?.destroy();
        this._tray = null;
        this.actor.destroy();
    }

    // For the headless test.
    get alerts() {
        return this._alerts;
    }

    // What the last look found, for the test and for bug reports.
    get status() {
        return this._status ?? 'not yet';
    }

    _loadNotified() {
        try {
            const list = JSON.parse(readText(this._stateFile) ?? '[]');
            return Array.isArray(list) ? list.filter(s => typeof s === 'string').slice(-MAX_NOTIFIED) : [];
        } catch {
            return [];
        }
    }

    _saveNotified() {
        try {
            GLib.mkdir_with_parents(GLib.path_get_dirname(this._stateFile), 0o700);
            GLib.file_set_contents(this._stateFile, JSON.stringify(this._notified.slice(-MAX_NOTIFIED)));
        } catch (e) {
            console.warn(`Vilna widgets: cannot remember warnings: ${e.message}`);
        }
    }

    _allowed() {
        return !this._monitor.network_metered || this._settings.get_boolean('alerts-metered');
    }

    // Look again now: on start, a new place, a new setting, unlock.
    update() {
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        this._refresh().catch(e => console.warn(`Vilna widgets: warnings: ${e.message}`));
    }

    _schedule(minutes) {
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, minutes * 60, () => {
            this._timer = 0;
            this._refresh().catch(e => console.warn(`Vilna widgets: warnings: ${e.message}`));
            return GLib.SOURCE_REMOVE;
        });
    }

    async _refresh() {
        const place = this._placeOf();
        if (!this._settings.get_boolean('weather-alerts') || !this._settings.get_boolean('weather') || !place) {
            this._show([], false);
            this._status = 'off';
            return;
        }
        if (Main.sessionMode.isLocked) {
            this._status = 'locked';
            this._schedule(REFRESH_MINUTES);
            return;
        }
        if (!this._allowed()) {
            this._status = 'metered';
            this._show([], true);
            this._schedule(REFRESH_MINUTES);
            return;
        }
        let ok = true;
        try {
            const zones = await this._zones(place);
            const country = zones[0]?.slice(0, 2) ?? this._homeCountry();
            const url = feedUrl(country);
            if (!url) {
                this._status = `no feed for ${country}`;
                this._show([], false);
                return;
            }
            const warnings = parseFeed(TEST_FEED ? readText(TEST_FEED) : await this._fetch(url));
            const alerts = alertsFor(warnings, place, zones, Date.now()).slice(0, MAX_SHOWN);
            for (const alert of alerts)
                alert.original = await this._original(alert);
            if (this._cancellable.is_cancelled())
                return;
            // The original at once; the translation, if any, when Gloxi has it.
            this._show(alerts, false);
            let translated = false;
            for (const alert of alerts) {
                alert.translation = await this._translation(alert);
                translated ||= Boolean(alert.translation);
            }
            if (this._cancellable.is_cancelled())
                return;
            if (translated)
                this._show(alerts, false);
            this._notify(alerts);
            this._status = `${alerts.length} of ${warnings.length}`;
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                return;
            ok = false;
            this._status = `error: ${e.message}`;
            console.warn(`Vilna widgets: no warnings: ${e.message}`);
        }
        this._schedule(ok ? REFRESH_MINUTES : RETRY_MINUTES);
    }

    async _fetch(url) {
        const message = Soup.Message.new('GET', url);
        const bytes = await this._session.send_and_read_async(message, GLib.PRIORITY_LOW, this._cancellable);
        if (message.get_status() !== Soup.Status.OK)
            throw new Error(`HTTP ${message.get_status()}`);
        return new TextDecoder().decode(bytes.get_data());
    }

    // The original text of a warning, fetched once (lib/alerts.js parseCap).
    async _original(alert) {
        if (this._caps.has(alert.id))
            return this._caps.get(alert.id);
        let original = null;
        try {
            if (alert.capUrl)
                original = parseCap(TEST_CAP ? readText(TEST_CAP) : await this._fetch(alert.capUrl), this._languages);
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                throw e;
            console.warn(`Vilna widgets: no warning text: ${e.message}`);
        }
        if (this._caps.size > 100)
            this._caps.clear();
        this._caps.set(alert.id, original);
        return original;
    }

    // The warning's words in the person's language by Gloxi, or null: when
    // they already are in it (Gloxi is not asked), or Gloxi cannot.
    async _translation(alert) {
        const words = wordsOf(alert);
        const target = translationTarget({headline: words.text, language: words.language}, this._languages);
        if (!target)
            return null;
        const key = `${target}\n${words.text}`;
        if (this._translations.has(key))
            return this._translations.get(key);
        let text = null;
        try {
            text = await this._askGloxi(words.text, target);
        } catch (e) {
            if (this._cancellable.is_cancelled())
                throw e;
            // Not cached: gloxid or its model may be back on the next look.
            console.log(`Vilna widgets: no translation of a warning: ${e.message}`);
            return null;
        }
        if (this._translations.size > 100)
            this._translations.clear();
        this._translations.set(key, text);
        return text;
    }

    // One text.translate request to gloxid. This connection is not a
    // window, so the Gloxi window keeps its events.
    async _askGloxi(text, language) {
        const cancellable = new Gio.Cancellable();
        const parent = this._cancellable.connect(() => cancellable.cancel());
        const timer = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, TRANSLATE_SECONDS, () => {
            cancellable.cancel();
            return GLib.SOURCE_REMOVE;
        });
        let conn = null;
        try {
            const client = new Gio.SocketClient();
            conn = await client.connect_async(Gio.UnixSocketAddress.new(gloxiSocket()), cancellable);
            const request = JSON.stringify({id: 1, method: 'text.translate', params: {text, language}});
            await conn.get_output_stream().write_bytes_async(new GLib.Bytes(new TextEncoder().encode(`${request}\n`)),
                GLib.PRIORITY_LOW, cancellable);
            const input = new Gio.DataInputStream({base_stream: conn.get_input_stream()});
            for (let i = 0; i < 1000; i++) {
                const [line] = await input.read_line_async(GLib.PRIORITY_LOW, cancellable);
                if (line === null)
                    throw new Error('gloxid closed the connection');
                const answer = JSON.parse(line);
                if (answer?.id !== 1)
                    continue; // an event, not our answer
                if (answer.error)
                    throw new Error(`${answer.error.code}: ${answer.error.message}`);
                const out = answer.result?.text;
                if (typeof out !== 'string' || !out.trim())
                    throw new Error('an empty translation');
                return out.trim().slice(0, 2000);
            }
            throw new Error('no answer from gloxid');
        } finally {
            GLib.source_remove(timer);
            this._cancellable.disconnect(parent);
            conn?.close(null);
        }
    }

    // The zones of the place, found once per place on this computer by
    // alertZones.js and kept in alert-zones.
    async _zones(place) {
        const [lat, lon, codes] = this._settings.get_value('alert-zones').deepUnpack();
        if (near(lat, place.lat) && near(lon, place.lon))
            return codes;
        if (this._zonesRunning)
            return [];
        const argv = ['gjs', '-m', this._dir.get_child('alertZones.js').get_path(), String(place.lat), String(place.lon)];
        if (TEST_GEOCODES)
            argv.push(TEST_GEOCODES);
        const proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
        this._zonesRunning = proc;
        try {
            const [stdout] = await proc.communicate_utf8_async(null, this._cancellable);
            if (!proc.get_successful())
                throw new Error(String(stdout ?? '').trim() || 'alertZones.js failed');
            const found = JSON.parse(stdout).filter(c => typeof c === 'string' && /^[A-Z]{2}[0-9A-Z]{1,10}$/.test(c));
            this._settings.set_value('alert-zones', new GLib.Variant('(ddas)', [place.lat, place.lon, found]));
            return found;
        } finally {
            this._zonesRunning = null;
        }
    }

    _clock(ms) {
        const d = GLib.DateTime.new_from_unix_local(Math.floor(ms / 1000));
        const today = GLib.DateTime.new_now_local();
        const sameDay = d && d.get_day_of_year() === today.get_day_of_year() && d.get_year() === today.get_year();
        return d?.format(sameDay ? '%H:%M' : '%a %H:%M') ?? '';
    }

    _show(alerts, paused) {
        this._alerts = alerts;
        this.actor.destroy_all_children();
        const t = this._text;
        if (paused) {
            const ask = new St.Button({style_class: 'vilna-alert vilna-alert-paused', x_expand: true,
                child: new St.Label({text: `${t.meteredPaused} · ${t.meteredAsk}`, x_expand: true})});
            wrap(ask.child);
            ask.connect('clicked', () => this._settings.set_boolean('alerts-metered', true));
            this.actor.add_child(ask);
        }
        const now = Date.now();
        for (const alert of alerts) {
            const box = verticalBox({x_expand: true});
            const when = alert.onset && alert.onset > now ? t.from.replace('%s', this._clock(alert.onset))
                : t.until.replace('%s', this._clock(alert.expires));
            box.add_child(wrap(new St.Label({text: `${alertLabel(alert, t)} · ${when}`, style_class: 'vilna-alert-title'})));
            // Gloxi's translation first, then the service's own words,
            // unchanged (MeteoAlarm terms).
            if (alert.translation) {
                box.add_child(wrap(new St.Label({text: alert.translation,
                    style_class: 'vilna-alert-translation'})));
            }
            box.add_child(wrap(new St.Label({text: wordsOf(alert).text,
                style_class: alert.translation ? 'vilna-alert-original vilna-alert-original-under'
                    : 'vilna-alert-original'})));
            const sender = alert.original?.sender || 'EUMETNET – MeteoAlarm';
            let source = t.issued.replace('%s', sender).replace('%s', this._clock(alert.sent ?? now));
            if (alert.translation)
                source += ` · ${t.translated}`;
            box.add_child(wrap(new St.Label({text: source, style_class: 'vilna-alert-source'})));
            const band = new St.Button({child: box, x_expand: true,
                style_class: `vilna-alert vilna-alert-${alert.colour}`});
            band.connect('clicked', () => this._open(alert.zoneUrl ?? SITE_URL));
            this.actor.add_child(band);
        }
        this.actor.visible = this.actor.get_n_children() > 0;
    }

    _open(uri) {
        try {
            Gio.AppInfo.launch_default_for_uri(uri, global.create_app_launch_context(0, -1));
        } catch (e) {
            console.warn(`Vilna widgets: cannot open ${uri}: ${e.message}`);
        }
    }

    // Our own source, so that the warning stays in the list until seen
    // (Main.notify's are transient).
    _source() {
        if (!this._tray) {
            this._tray = new MessageTray.Source({title: this._text.title, iconName: 'dialog-warning-symbolic'});
            this._tray.connect('destroy', () => (this._tray = null));
            Main.messageTray.add(this._tray);
        }
        return this._tray;
    }

    _notify(alerts) {
        const fresh = toNotify(alerts, this._notified);
        if (!fresh.length)
            return;
        for (const alert of fresh) {
            const original = wordsOf(alert).text;
            const sender = alert.original?.sender || 'EUMETNET – MeteoAlarm';
            const words = alert.translation
                ? `${alert.translation}\n${original}\n${sender} · ${this._text.translated}`
                : `${original}\n${sender}`;
            const source = this._source();
            const notification = new MessageTray.Notification({
                source,
                title: alertLabel(alert, this._text),
                body: words,
                urgency: alert.level >= 4 ? MessageTray.Urgency.CRITICAL : MessageTray.Urgency.HIGH,
            });
            notification.connect('activated', () => this._open(alert.zoneUrl ?? SITE_URL));
            source.addNotification(notification);
            this._notified.push(alert.id);
        }
        this._saveNotified();
    }
}

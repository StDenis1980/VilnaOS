// SPDX-License-Identifier: GPL-3.0-or-later
//
// (з) The clock card of the desktop widgets: the time and date, the next
// alarm and up to two other time zones from GNOME Clocks, and a menu that
// opens Clocks on its alarms, world clocks, stopwatch or timer, or the
// system's date and time settings. lib/clockInfo.js has the logic and
// says where Clocks keeps its settings.
//
// Nothing goes to the network. Clocks' settings are read when its file
// changes; the only thing written is the tab Clocks opens on, chosen in
// the menu. Clocks rings only while it runs, so with an alarm set and
// Clocks closed the card says so.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GWeather from 'gi://GWeather?version=4.0';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {
    CLOCKS_APP, CLOCKS_BUS_NAME, CLOCKS_GROUP, CLOCKS_STATE_GROUP, FLATPAK_KEYFILE, clockState, nextAlarm,
    whenKind, zoneRows,
} from './lib/clockInfo.js';
import {verticalBox} from './vbox.js';

const DATE_TIME_PANEL = 'gnome-datetime-panel.desktop';
// Adwaita has an alarm clock; the other three are drawn for Vilna (icons/).
const MENU = [
    ['alarm', 'alarms', 'alarm'],
    ['world', 'world', 'world'],
    ['stopwatch', 'stopwatch', 'stopwatch'],
    ['timer', 'timer', 'timer'],
];

function label(text, style, params = {}) {
    return new St.Label({text, style_class: style, y_align: Clutter.ActorAlign.CENTER, ...params});
}


// An installed GSettings schema, or null.
function lookupSchema(id) {
    return Gio.SettingsSchemaSource.get_default()?.lookup(id, true) ?? null;
}

export class ClockCard {
    // settings: the extension's; text: clockText(); dir: the extension's
    // directory (a Gio.File), for its icons.
    constructor(settings, text, dir) {
        this._settings = settings;
        this._dir = dir;
        this._t = text;
        this._alarms = [];
        this._zones = [];
        this._clocksRunning = false;

        this._time = label('', 'vilna-widget-time', {x_expand: true});
        this._date = label('', 'vilna-widget-date', {x_expand: true});
        this._extra = verticalBox({style_class: 'vilna-clock-extra', x_expand: true});
        const box = verticalBox({x_expand: true});
        box.add_child(this._time);
        box.add_child(this._date);
        box.add_child(this._extra);
        this.actor = new St.Button({
            style_class: 'vilna-widget vilna-widget-clock', child: box, x_expand: true,
            can_focus: true, reactive: true, track_hover: true, accessible_name: text.menu,
        });
        this.actor.name = 'vilnaClock';
        this.actor.connect('clicked', () => this._toggleMenu());
        this._buildMenu();

        this._home = GLib.get_home_dir();
        this._keyfilePath = GLib.build_filenamev([this._home, ...FLATPAK_KEYFILE]);
        this._keyfile = Gio.File.new_for_path(this._keyfilePath);
        try {
            this._monitor = this._keyfile.monitor_file(Gio.FileMonitorFlags.NONE, null);
            this._monitorId = this._monitor.connect('changed', () => this._read());
        } catch (e) {
            console.warn(`Vilna clock: cannot watch the Clocks settings: ${e.message}`);
        }
        // Clocks installed as a package (not a Flatpak) uses dconf.
        if (lookupSchema('org.gnome.clocks')) {
            this._clocksSettings = new Gio.Settings({schema_id: 'org.gnome.clocks'});
            this._clocksSettingsId = this._clocksSettings.connect('changed', () => this._read());
        }
        // The Shell's copy of the world clocks, kept while Clocks runs.
        if (lookupSchema('org.gnome.shell.world-clocks')) {
            this._shellClocks = new Gio.Settings({schema_id: 'org.gnome.shell.world-clocks'});
            this._shellClocksId = this._shellClocks.connect('changed::locations', () => this._read());
        }
        this._settingsIds = ['changed::clock-alarm', 'changed::clock-zones']
            .map(signal => this._settings.connect(signal, () => this._sync()));
        this._watchId = Gio.bus_watch_name(Gio.BusType.SESSION, CLOCKS_BUS_NAME, Gio.BusNameWatcherFlags.NONE,
            () => this._setRunning(true), () => this._setRunning(false));
        this._read();
    }

    destroy() {
        if (this._watchId)
            Gio.bus_unwatch_name(this._watchId);
        this._watchId = 0;
        this._settingsIds.forEach(id => this._settings.disconnect(id));
        this._settingsIds = [];
        if (this._monitor) {
            this._monitor.disconnect(this._monitorId);
            this._monitor.cancel();
            this._monitor = null;
        }
        this._clocksSettings?.disconnect(this._clocksSettingsId);
        this._clocksSettings = null;
        this._shellClocks?.disconnect(this._shellClocksId);
        this._shellClocks = null;
        this._manager?.removeMenu(this._menu);
        this._menu?.destroy();
        this._menu = null;
        this.actor.destroy();
    }

    // For the headless test.
    get menu() {
        return this._menu;
    }

    get zones() {
        return this._zones.map(z => z.name);
    }

    // Called every minute and when the 12/24-hour setting changes.
    tick(twelve) {
        this._twelve = twelve;
        const now = GLib.DateTime.new_now_local();
        this._time.text = now.format(twelve ? '%l:%M' : '%H:%M').trim();
        const date = now.format('%A, %-d %B') ?? '';
        this._date.text = date.charAt(0).toLocaleUpperCase() + date.slice(1);
        this._sync();
    }

    _setRunning(running) {
        this._clocksRunning = running;
        this._sync();
    }

    _gicon(name) {
        if (name === 'alarm')
            return Gio.ThemedIcon.new_from_names(['alarm-symbolic', 'preferences-system-time-symbolic']);
        const file = this._dir?.get_child('icons').get_child(`vilna-clock-${name}-symbolic.svg`);
        return file ? new Gio.FileIcon({file}) : Gio.ThemedIcon.new('preferences-system-time-symbolic');
    }

    _hhmm(dateTime) {
        return dateTime.format(this._twelve ? '%l:%M' : '%H:%M').trim();
    }

    // Clocks' alarms and world clocks, from its keyfile (Flatpak) or dconf.
    _read() {
        const keyfile = new GLib.KeyFile();
        let fromFile = false;
        try {
            fromFile = keyfile.load_from_file(this._keyfilePath, GLib.KeyFileFlags.NONE);
        } catch {
            fromFile = false;
        }
        const value = key => {
            if (fromFile) {
                try {
                    return GLib.Variant.parse(new GLib.VariantType('aa{sv}'), keyfile.get_value(CLOCKS_GROUP, key), null, null);
                } catch {
                    // Not in the file: Clocks keeps its default, an empty list.
                }
            }
            return this._clocksSettings?.get_value(key) ?? null;
        };

        const alarms = value('alarms');
        try {
            this._alarms = alarms?.recursiveUnpack() ?? [];
        } catch {
            this._alarms = [];
        }

        let locations = [];
        const world = value('world-clocks');
        for (let i = 0; i < (world?.n_children() ?? 0); i++) {
            const location = world.get_child_value(i).lookup_value('location', null);
            if (location)
                locations.push(location);
        }
        if (!locations.length && this._shellClocks)
            locations = this._shellClocks.get_value('locations').deepUnpack();
        this._zones = [];
        const earth = GWeather.Location.get_world();
        for (const v of locations) {
            try {
                const place = earth?.deserialize(v);
                const zone = place?.get_timezone();
                if (zone)
                    this._zones.push({name: place.get_city_name() || place.get_name(), zone});
            } catch (e) {
                console.warn(`Vilna clock: a world clock could not be read: ${e.message}`);
            }
        }
        this._sync();
    }

    _sync() {
        if (!this._extra)
            return;
        this._extra.destroy_all_children();
        const now = new Date();
        const alarm = this._settings.get_boolean('clock-alarm') ? nextAlarm(this._alarms, now) : null;
        if (alarm) {
            const at = GLib.DateTime.new_from_unix_local(Math.floor(alarm.at.getTime() / 1000));
            const kind = whenKind(alarm.at, now);
            const when = kind === 'today' ? this._t.today : kind === 'tomorrow' ? this._t.tomorrow
                : at.format(kind === 'week' ? '%A' : '%-d %B') ?? '';
            this._row('vilnaClockAlarm', 'alarm', this._hhmm(at), alarm.name ? `${when} · ${alarm.name}` : when);
            if (!this._clocksRunning)
                this._extra.add_child(label(this._t.closed, 'vilna-widget-line vilna-clock-warning', {x_expand: true}));
        }
        if (this._settings.get_boolean('clock-zones')) {
            const local = GLib.DateTime.new_now_local();
            const zones = this._zones.map(z => ({name: z.name, offset: GLib.DateTime.new_now(z.zone).get_utc_offset() / 1e6, zone: z.zone}));
            for (const row of zoneRows(zones, local.get_utc_offset() / 1e6)) {
                const zone = zones.find(z => z.name === row.name).zone;
                this._row('vilnaClockZone', 'world', this._hhmm(GLib.DateTime.new_now(zone)),
                    `${row.name} · ${this._t.hours.replace('%s', row.diff)}`);
            }
        }
        // A thin line between the date and the rows, only when there are rows.
        if (this._extra.get_n_children())
            this._extra.add_style_class_name('vilna-clock-extra-shown');
        else
            this._extra.remove_style_class_name('vilna-clock-extra-shown');
    }

    _row(name, iconName, time, text) {
        const row = new St.BoxLayout({style_class: 'vilna-clock-row', x_expand: true});
        row.name = name;
        row.add_child(new St.Icon({gicon: this._gicon(iconName), style_class: 'vilna-clock-icon',
            y_align: Clutter.ActorAlign.CENTER}));
        row.add_child(label(time, 'vilna-clock-row-time'));
        row.add_child(label(text, 'vilna-widget-line', {x_expand: true}));
        this._extra.add_child(row);
    }

    _buildMenu() {
        // Under the card, starting near its left edge, so that it stays in
        // the column of widgets.
        this._menu = new PopupMenu.PopupMenu(this.actor, 0.1, St.Side.TOP);
        this._menu.setSourceAlignment(0.15);
        this._menu.actor.add_style_class_name('vilna-clock-menu');
        Main.uiGroup.add_child(this._menu.actor);
        this._menu.actor.hide();
        this._manager = new PopupMenu.PopupMenuManager(this.actor);
        this._manager.addMenu(this._menu);
        for (const [panel, key, iconName] of MENU) {
            const item = new PopupMenu.PopupImageMenuItem(this._t[key], this._gicon(iconName));
            item.connect('activate', () => this._openClocks(panel));
            this._menu.addMenuItem(item);
        }
        this._menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const settings = new PopupMenu.PopupImageMenuItem(this._t.dateTime,
            Gio.ThemedIcon.new('preferences-system-time-symbolic'));
        settings.connect('activate', () => this._openDateTime());
        this._menu.addMenuItem(settings);
    }

    _toggleMenu() {
        this._menu.toggle();
        // Mouse or keyboard alike: the first item takes the focus, so that
        // the arrows, Enter and Escape work at once.
        if (this._menu.isOpen)
            this._menu.actor.navigate_focus(null, St.DirectionType.TAB_FORWARD, false);
    }

    // Opens Clocks on a tab: Clocks opens on the tab its settings name, so
    // that one value is set first. A window already open keeps its tab.
    _openClocks(panel) {
        const app = Shell.AppSystem.get_default().lookup_app(CLOCKS_APP);
        if (!app) {
            console.warn(`Vilna clock: ${CLOCKS_APP} is not installed`);
            return;
        }
        const state = clockState(panel);
        try {
            if (state && app.get_app_info()?.get_string('X-Flatpak')) {
                const keyfile = new GLib.KeyFile();
                try {
                    keyfile.load_from_file(this._keyfilePath, GLib.KeyFileFlags.KEEP_COMMENTS);
                } catch {
                    // No file yet: Clocks has not run. Start one.
                }
                keyfile.set_value(CLOCKS_STATE_GROUP, 'panel-id', state);
                GLib.mkdir_with_parents(GLib.path_get_dirname(this._keyfilePath), 0o700);
                keyfile.save_to_file(this._keyfilePath);
            } else if (state && lookupSchema('org.gnome.clocks.state.window')) {
                new Gio.Settings({schema_id: 'org.gnome.clocks.state.window'}).set_string('panel-id', panel);
            }
        } catch (e) {
            console.warn(`Vilna clock: cannot choose the Clocks tab: ${e.message}`);
        }
        app.activate();
    }

    _openDateTime() {
        const panel = Shell.AppSystem.get_default().lookup_app(DATE_TIME_PANEL);
        if (panel) {
            panel.activate();
            return;
        }
        try {
            GLib.spawn_async(null, ['gnome-control-center', 'system', 'datetime'], null, GLib.SpawnFlags.SEARCH_PATH, null);
        } catch (e) {
            console.warn(`Vilna clock: cannot open the date and time settings: ${e.message}`);
        }
    }
}

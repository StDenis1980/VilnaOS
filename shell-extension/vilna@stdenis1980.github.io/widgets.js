// SPDX-License-Identifier: GPL-3.0-or-later
//
// (з) Desktop widgets (docs/design/desktop/README.md): a column of cards
// on the left of the desktop, above the wallpaper and under every window.
// The clock and date with the next alarm and other time zones
// (clockCard.js), Gloxi (a question goes to the Gloxi app), today in
// the calendar (GNOME's own calendar server, nothing leaves the computer),
// the weather and the state of Копії (vilna-backup's own log).
//
// The weather is for the capital of the system's country unless the
// person picked a place in the preferences (lib/widgets.js says why): the
// only request is the forecast for that place, to Open-Meteo, every 30
// minutes while the widgets are on. Behind the card a live sky
// (sky.js) shows the weather, and stops whenever a window covers it.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GnomeDesktop from 'gi://GnomeDesktop';
import Shell from 'gi://Shell';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';

import * as Calendar from 'resource:///org/gnome/shell/ui/calendar.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {
    REFRESH_MINUTES, RETRY_MINUTES, askArgv, backupWhen, forecastUrl, homeCountry, lastBackup,
    parseForecast, todayEvents, weatherPlace, weatherScene, widgetText,
} from './lib/widgets.js';
import {clockText} from './lib/clockInfo.js';
import {skyColours} from './lib/weatherSky.js';
import {ClockCard} from './clockCard.js';
import {Sky} from './sky.js';
import {WeatherAlerts} from './weatherAlerts.js';
import {verticalBox} from './vbox.js';

const GLOXI = 'io.github.stdenis1980.Gloxi.desktop';
const BACKUP = 'io.github.stdenis1980.VilnaBackup.desktop';
const WIDTH = 340;
const MARGIN = 24;
// Tests only: a JSON file read instead of asking Open-Meteo, so that the
// headless Shell test checks the whole weather path without the network.
const WEATHER_FILE = GLib.getenv('VILNA_TEST_WEATHER_FILE');

function label(text, style) {
    return new St.Label({text, style_class: style, x_expand: true});
}

function card(name, title) {
    const box = verticalBox({style_class: 'vilna-widget', x_expand: true});
    if (name)
        box.name = name;
    if (title)
        box.add_child(label(title.toUpperCase(), 'vilna-widget-title'));
    return box;
}

function readText(path) {
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        return ok ? new TextDecoder().decode(bytes) : null;
    } catch {
        return null;
    }
}

export class Widgets {
    // dir: the extension's directory, for alertZones.js.
    constructor(settings, openPreferences, dir) {
        this._settings = settings;
        this._openPreferences = openPreferences;
        this._dir = dir;
        this._root = null;
    }

    enable() {
        this._text = widgetText(GLib.get_language_names());
        this._settingsIds = ['changed::widgets', 'changed::weather', 'changed::weather-location']
            .map(signal => this._settings.connect(signal, () => this._sync()));
        this._sync();
    }

    disable() {
        for (const id of this._settingsIds ?? [])
            this._settings.disconnect(id);
        this._settingsIds = [];
        this._hide();
    }

    // For the headless test.
    get root() {
        return this._root;
    }

    _sync() {
        if (!this._settings.get_boolean('widgets')) {
            this._hide();
            return;
        }
        if (!this._root)
            this._show();
        this._weatherCard.visible = this._settings.get_boolean('weather');
        this._refreshWeather();
        this._alerts.update();
    }

    _show() {
        const t = this._text;
        this._root = verticalBox({style_class: 'vilna-widgets', width: WIDTH});
        this._root.name = 'vilnaWidgets';

        // Clock and date, the next alarm and other time zones; a menu
        // opens Clocks.
        this._clock = new ClockCard(this._settings, clockText(GLib.get_language_names()), this._dir);
        this._root.add_child(this._clock.actor);

        // Gloxi: the field opens Gloxi; a suggestion asks it at once.
        const gloxi = card('vilnaGloxiWidget', t.gloxi);
        gloxi.add_child(label(t.help, 'vilna-widget-heading'));
        const field = new St.Button({style_class: 'vilna-widget-field', x_expand: true,
            child: new St.Label({text: t.askPlaceholder, x_align: Clutter.ActorAlign.START, x_expand: true})});
        field.connect('clicked', () => this._launch(GLOXI));
        gloxi.add_child(field);
        // Two suggestions to a row, as in the mockup: long words fit.
        let chips = null;
        t.chips.forEach((question, i) => {
            if (i % 2 === 0) {
                chips = new St.BoxLayout({style_class: 'vilna-widget-chips'});
                gloxi.add_child(chips);
            }
            const chip = new St.Button({label: question, style_class: 'vilna-widget-chip'});
            chip.connect('clicked', () => this._ask(question));
            chips.add_child(chip);
        });
        this._root.add_child(gloxi);

        // Today in the calendar.
        this._events = card('vilnaCalendarWidget', t.today);
        this._eventList = verticalBox();
        this._events.add_child(this._eventList);
        this._root.add_child(this._events);

        // Weather: the card over its live sky.
        this._weatherCard = new St.Widget({layout_manager: new Clutter.BinLayout(), x_expand: true});
        this._weatherCard.name = 'vilnaWeatherWidget';
        this._sky = new Sky();
        this._sky.actor.visible = false;
        this._weatherCard.add_child(this._sky.actor);
        this._weatherContent = card(null, t.weather);
        this._weatherCard.add_child(this._weatherContent);
        const row = new St.BoxLayout({x_expand: true});
        const temps = verticalBox({x_expand: true});
        this._temp = label('—', 'vilna-widget-temp');
        this._weatherLine = label(t.noWeather, 'vilna-widget-line');
        temps.add_child(this._temp);
        temps.add_child(this._weatherLine);
        this._weatherIcon = new St.Icon({icon_name: 'weather-overcast-symbolic', style_class: 'vilna-widget-weather-icon',
            y_align: Clutter.ActorAlign.CENTER});
        row.add_child(temps);
        row.add_child(this._weatherIcon);
        const weatherButton = new St.Button({child: row, x_expand: true, style_class: 'vilna-widget-plain'});
        weatherButton.connect('clicked', () => this._openPreferences?.());
        this._weatherContent.add_child(weatherButton);
        // Warnings below the forecast (weatherAlerts.js).
        this._alerts = new WeatherAlerts(this._settings, this._dir, () => this.currentPlace(), () => this._country());
        this._weatherContent.add_child(this._alerts.actor);
        this._root.add_child(this._weatherCard);

        // Копії.
        this._backupTitle = label('', 'vilna-widget-heading-small');
        this._backupLine = label('', 'vilna-widget-line');
        const backupBox = verticalBox({x_expand: true});
        backupBox.add_child(this._backupTitle);
        backupBox.add_child(this._backupLine);
        const backupButton = new St.Button({child: backupBox, x_expand: true, style_class: 'vilna-widget-plain'});
        backupButton.connect('clicked', () => this._launch(BACKUP));
        const backup = card('vilnaBackupWidget');
        backup.add_child(backupButton);
        this._root.add_child(backup);

        // The wallpapers live in their own group inside the window group, at
        // its bottom; the widgets go on top of them there, so that every
        // window covers them.
        Main.layoutManager._backgroundGroup.add_child(this._root);
        this._place();

        this._wallClock = new GnomeDesktop.WallClock();
        this._interface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
        this._clockIds = [
            this._wallClock.connect('notify::clock', () => this._tick()),
            this._interface.connect('changed::clock-format', () => this._tick()),
        ];
        this._tick();

        this._calendar = new Calendar.DBusEventSource();
        this._calendarId = this._calendar.connect('changed', () => this._syncEvents());
        this._syncEvents();

        this._backupFile = Gio.File.new_for_path(GLib.build_filenamev([GLib.get_user_state_dir(), 'vilna-backup', 'runs.jsonl']));
        try {
            this._backupMonitor = this._backupFile.monitor_file(Gio.FileMonitorFlags.NONE, null);
            this._backupMonitorId = this._backupMonitor.connect('changed', () => this._syncBackup());
        } catch (e) {
            console.warn(`Vilna widgets: cannot watch the Копії log: ${e.message}`);
        }
        this._syncBackup();

        this._monitorsId = Main.layoutManager.connect('monitors-changed', () => this._place());
        // A dock on the left (dock-position LEFT) would cover the column:
        // follow the docks Dash to Dock builds and keep to the right of one.
        this._docks = new Map();
        this._dockIds = [
            Main.uiGroup.connect('child-added', (_g, actor) => this._watchDock(actor)),
            Main.uiGroup.connect('child-removed', (_g, actor) => this._forgetDock(actor)),
        ];
        for (const actor of Main.uiGroup.get_children())
            this._watchDock(actor);
        this._overviewIds = [
            Main.overview.connect('showing', () => (this._root.visible = false)),
            Main.overview.connect('hidden', () => (this._root.visible = true)),
        ];
        this._root.visible = !Main.overview.visible;
        this._session = new Soup.Session({user_agent: 'Vilna desktop widgets', timeout: 20});
        this._cancellable = new Gio.Cancellable();
    }

    _hide() {
        if (!this._root)
            return;
        this._cancellable?.cancel();
        this._cancellable = null;
        if (this._weatherTimer)
            GLib.source_remove(this._weatherTimer);
        this._weatherTimer = 0;
        this._session?.abort();
        this._session = null;
        Main.layoutManager.disconnect(this._monitorsId);
        this._dockIds.forEach(id => Main.uiGroup.disconnect(id));
        this._dockIds = [];
        for (const [dock, ids] of this._docks)
            ids.forEach(id => dock.disconnect(id));
        this._docks.clear();
        for (const id of this._overviewIds)
            Main.overview.disconnect(id);
        this._wallClock.disconnect(this._clockIds[0]);
        this._interface.disconnect(this._clockIds[1]);
        this._wallClock.run_dispose?.();
        this._wallClock = null;
        this._clock.destroy();
        this._clock = null;
        this._calendar.disconnect(this._calendarId);
        this._calendar.destroy();
        this._calendar = null;
        if (this._backupMonitor) {
            this._backupMonitor.disconnect(this._backupMonitorId);
            this._backupMonitor.cancel();
        }
        this._backupMonitor = null;
        this._alerts.destroy();
        this._alerts = null;
        this._sky.destroy();
        this._sky = null;
        this._root.destroy();
        this._root = null;
    }

    _watchDock(actor) {
        if (actor?.name !== 'dashtodockContainer' || this._docks.has(actor))
            return;
        this._docks.set(actor, [
            actor.connect('notify::width', () => this._place()),
            actor.connect('notify::x', () => this._place()),
            actor.connect('notify::style-class', () => this._place()),
        ]);
        this._place();
    }

    _forgetDock(actor) {
        const ids = this._docks?.get(actor);
        if (!ids)
            return;
        ids.forEach(id => actor.disconnect(id));
        this._docks.delete(actor);
        this._place();
    }

    // Width of a dock on the left edge of the monitor, or 0.
    _leftDock(monitor) {
        let width = 0;
        for (const dock of this._docks?.keys() ?? []) {
            if (dock.has_style_class_name('left') && dock.x >= monitor.x && dock.x < monitor.x + monitor.width / 2)
                width = Math.max(width, dock.x - monitor.x + dock.width);
        }
        return width;
    }

    _place() {
        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor || !this._root)
            return;
        // New monitors bring new wallpapers; stay above them.
        this._root.get_parent()?.set_child_above_sibling(this._root, null);
        this._root.set_position(monitor.x + this._leftDock(monitor) + MARGIN, monitor.y + Main.panel.height + MARGIN);
    }

    _tick() {
        const now = GLib.DateTime.new_now_local();
        this._clock.tick(this._interface.get_string('clock-format') === '12h');
        // A new day: today's events change too.
        if (this._day !== now.get_day_of_year()) {
            this._day = now.get_day_of_year();
            this._syncEvents();
        }
        if (this._backupFile)
            this._syncBackup();
    }

    _syncEvents() {
        if (!this._calendar)
            return;
        const now = new Date();
        const begin = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const end = new Date(begin.getTime() + 86400000);
        this._calendar.requestRange(begin, end);
        const events = todayEvents(this._calendar.getEvents(begin, end), now);
        this._eventList.destroy_all_children();
        if (!events.length) {
            this._eventList.add_child(label(this._text.nothing, 'vilna-widget-line'));
            return;
        }
        const time = d => GLib.DateTime.new_from_unix_local(Math.floor(d.getTime() / 1000)).format('%H:%M');
        for (const e of events) {
            const box = verticalBox({style_class: 'vilna-widget-event'});
            box.add_child(label(e.summary, 'vilna-widget-event-title'));
            box.add_child(label(e.allDay ? this._text.allDay : `${time(e.start)}–${time(e.end)}`, 'vilna-widget-line'));
            this._eventList.add_child(box);
        }
    }

    _syncBackup() {
        const t = this._text;
        const last = lastBackup(readText(this._backupFile.get_path()));
        if (!last) {
            this._backupTitle.text = t.backupNone;
            this._backupLine.text = '';
            this._backupLine.visible = false;
            return;
        }
        this._backupTitle.text = last.ok ? t.backupOk : t.backupFailed;
        const local = s => GLib.DateTime.new_from_unix_local(Math.floor(s));
        const now = GLib.DateTime.new_now_local();
        const [when, value] = backupWhen(last.time, now.to_unix(), now.get_utc_offset() / 1e6,
            s => local(s).format('%H:%M'), s => local(s).format('%-d %B'));
        this._backupLine.text = t.last[when].replace('%s', value);
        this._backupLine.visible = true;
    }

    // The place: the person's choice, else the capital of the system's country.
    currentPlace() {
        const [name, lat, lon] = this._settings.get_value('weather-location').deepUnpack();
        return weatherPlace([name, lat, lon], this._country());
    }

    _country() {
        const region = new Gio.Settings({schema_id: 'org.gnome.system.locale'}).get_string('region');
        return homeCountry({
            zone: GLib.TimeZone.new_local().get_identifier(),
            zoneTab: readText('/usr/share/zoneinfo/zone.tab'),
            region,
            lang: GLib.getenv('LANG'),
        });
    }

    // For the headless test.
    get weatherAlerts() {
        return this._alerts;
    }

    async _refreshWeather() {
        if (!this._root || !this._settings.get_boolean('weather'))
            return;
        if (this._weatherTimer)
            GLib.source_remove(this._weatherTimer);
        this._weatherTimer = 0;
        const place = this.currentPlace();
        if (!place) {
            this._showWeather(null, null);
            return;
        }
        let forecast = null;
        try {
            forecast = parseForecast(await this._fetch(forecastUrl(place)));
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                return;
            console.warn(`Vilna widgets: no forecast: ${e.message}`);
        }
        if (!this._root)
            return;
        this._showWeather(place, forecast);
        const minutes = forecast ? REFRESH_MINUTES : RETRY_MINUTES;
        this._weatherTimer = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, minutes * 60, () => {
            this._weatherTimer = 0;
            this._refreshWeather();
            return GLib.SOURCE_REMOVE;
        });
    }

    async _fetch(url) {
        if (WEATHER_FILE)
            return readText(WEATHER_FILE) ?? '';
        const message = Soup.Message.new('GET', url);
        const bytes = await this._session.send_and_read_async(message, GLib.PRIORITY_LOW, this._cancellable);
        if (message.get_status() !== Soup.Status.OK)
            throw new Error(`HTTP ${message.get_status()}`);
        return new TextDecoder().decode(bytes.get_data());
    }

    _showWeather(place, forecast) {
        const t = this._text;
        const scene = place ? weatherScene(forecast) : null;
        this._setSky(scene);
        if (!place) {
            this._temp.text = '—';
            this._weatherLine.text = t.chooseCity;
            return;
        }
        if (!scene) {
            this._temp.text = '—';
            this._weatherLine.text = `${place.name} · ${t.noWeather}`;
            return;
        }
        this._temp.text = `${forecast.temp}°`;
        const range = forecast.high !== null && forecast.low !== null ? ` · ↑${forecast.high}° ↓${forecast.low}°` : '';
        const wind = scene.kind === 'wind' && forecast.gusts !== null ? ` · ${forecast.gusts} km/h` : '';
        this._weatherLine.text = `${place.name} · ${t.kinds[scene.index]}${wind}${range}`;
        this._weatherIcon.icon_name = scene.icon;
    }

    // The sky behind the card, and text that reads on it; without a
    // forecast the card is plain glass like the others.
    _setSky(scene) {
        const content = this._weatherContent;
        for (const name of ['vilna-widget-on-sky', 'vilna-sky-ink-dark', 'vilna-sky-ink-light'])
            content.remove_style_class_name(name);
        this._sky.actor.visible = Boolean(scene);
        // The sky itself shows the weather, the sun or moon where the icon was.
        this._weatherIcon.visible = !scene;
        if (!scene)
            return;
        this._sky.show(scene.kind, scene.day, scene.strength);
        content.add_style_class_name('vilna-widget-on-sky');
        content.add_style_class_name(`vilna-sky-ink-${skyColours(scene.kind, scene.day).ink}`);
    }

    // For the headless test.
    get clock() {
        return this._clock;
    }

    get sky() {
        return this._sky;
    }

    _launch(desktopId) {
        const app = Shell.AppSystem.get_default().lookup_app(desktopId);
        if (app)
            app.activate();
        else
            console.warn(`Vilna widgets: ${desktopId} is not installed`);
    }

    _ask(question) {
        const info = Gio.DesktopAppInfo.new(GLOXI);
        if (!info) {
            console.warn('Vilna widgets: Gloxi is not installed');
            return;
        }
        try {
            const [, argv] = GLib.shell_parse_argv(info.get_commandline());
            const command = askArgv(argv, question);
            if (command)
                Gio.Subprocess.new(command, Gio.SubprocessFlags.NONE);
        } catch (e) {
            console.warn(`Vilna widgets: cannot ask Gloxi: ${e.message}`);
        }
    }
}

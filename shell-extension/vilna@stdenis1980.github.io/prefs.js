// SPDX-License-Identifier: GPL-3.0-or-later
//
// (з) Preferences of the Vilna extension: the desktop widgets, the
// place for the weather and the sounds (theme, and each sound on or off).
// The weather is for the capital of
// the system's country and nothing about the person's location leaves
// the computer. To change it the person either types a town (only that
// text goes to Open-Meteo's search) or, after agreeing in a dialog, lets
// the system find their approximate position, rounded to about 10 km.

import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk';
import Soup from 'gi://Soup?version=3.0';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

import {
    geocodeUrl, homeCountry, languageOf, parseGeocode, roundForPrivacy, weatherPlace, widgetText,
} from './lib/widgets.js';
import {DISCLAIMER, SITE_URL, alertText} from './lib/alerts.js';
import {SOUND_EVENTS, SOUND_THEMES, soundText} from './lib/soundTheme.js';

const APP_ID = 'org.gnome.Shell.Extensions';

function readText(path) {
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        return ok ? new TextDecoder().decode(bytes) : null;
    } catch {
        return null;
    }
}

function country() {
    return homeCountry({
        zone: GLib.TimeZone.new_local().get_identifier(),
        zoneTab: readText('/usr/share/zoneinfo/zone.tab'),
        region: new Gio.Settings({schema_id: 'org.gnome.system.locale'}).get_string('region'),
        lang: GLib.getenv('LANG'),
    });
}

export default class VilnaPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const t = widgetText(GLib.get_language_names());
        const language = languageOf(GLib.get_language_names());
        const session = new Soup.Session({user_agent: 'Vilna desktop widgets', timeout: 20});
        const cancellable = new Gio.Cancellable();
        window.connect('close-request', () => {
            cancellable.cancel();
            session.abort();
            return false;
        });

        const page = new Adw.PreferencesPage({title: t.prefsTitle, icon_name: 'preferences-desktop-symbolic'});
        window.add(page);

        const shown = new Adw.PreferencesGroup({title: t.prefsTitle});
        for (const [key, title] of [['widgets', t.showWidgets], ['weather', t.showWeather]]) {
            const row = new Adw.SwitchRow({title});
            settings.bind(key, row, 'active', Gio.SettingsBindFlags.DEFAULT);
            shown.add(row);
        }
        page.add(shown);

        const where = new Adw.PreferencesGroup({title: t.place, description: t.searchHint});
        settings.bind('weather', where, 'sensitive', Gio.SettingsBindFlags.GET);
        const current = new Adw.ActionRow({title: '', subtitle: t.dataBy});
        current.add_css_class('property');
        where.add(current);

        const capitalButton = new Gtk.Button({label: t.backToCapital, valign: Gtk.Align.CENTER});
        capitalButton.connect('clicked', () => settings.set_value('weather-location', new GLib.Variant('(sdd)', ['', 0, 0])));
        current.add_suffix(capitalButton);

        const showCurrent = () => {
            const [name, lat, lon] = settings.get_value('weather-location').deepUnpack();
            const place = weatherPlace([name, lat, lon], country());
            current.title = !place ? t.chooseCity
                : place.capital ? t.capital.replace('%s', place.name) : place.name;
            capitalButton.visible = Boolean(place && !place.capital);
        };
        settings.connect('changed::weather-location', showCurrent);
        showCurrent();

        const choose = (name, lat, lon) =>
            settings.set_value('weather-location', new GLib.Variant('(sdd)', [name, lat, lon]));

        // Search: the text typed goes to Open-Meteo's geocoding, nothing else.
        const search = new Gtk.SearchEntry({placeholder_text: t.search, margin_top: 12});
        where.add(search);
        const results = new Gtk.ListBox({selection_mode: Gtk.SelectionMode.NONE, css_classes: ['boxed-list'], margin_top: 6});
        results.visible = false;
        where.add(results);
        let token = 0;
        search.connect('activate', async () => {
            const query = search.text.trim();
            const mine = ++token;
            results.remove_all();
            if (!query) {
                results.visible = false;
                return;
            }
            let found = [];
            try {
                const message = Soup.Message.new('GET', geocodeUrl(query, language));
                const bytes = await session.send_and_read_async(message, GLib.PRIORITY_DEFAULT, cancellable);
                found = parseGeocode(new TextDecoder().decode(bytes.get_data()));
            } catch (e) {
                console.warn(`Vilna prefs: town search failed: ${e.message}`);
            }
            if (mine !== token)
                return;
            if (!found.length)
                results.append(new Adw.ActionRow({title: t.notFound}));
            for (const r of found) {
                const row = new Adw.ActionRow({title: r.name, subtitle: r.detail, activatable: true});
                row.connect('activated', () => {
                    choose(r.name, r.lat, r.lon);
                    results.remove_all();
                    results.visible = false;
                    search.text = '';
                });
                results.append(row);
            }
            results.visible = true;
        });

        // My location: only after the person agrees, and rounded.
        const mineButton = new Gtk.Button({label: t.useMine, halign: Gtk.Align.START, margin_top: 12});
        mineButton.add_css_class('pill');
        where.add(mineButton);
        const status = new Gtk.Label({wrap: true, xalign: 0, margin_top: 6, visible: false, css_classes: ['dim-label']});
        where.add(status);
        mineButton.connect('clicked', () => {
            const dialog = new Adw.AlertDialog({heading: t.consentTitle, body: t.consentBody});
            dialog.add_response('cancel', t.cancel);
            dialog.add_response('send', t.send);
            dialog.set_response_appearance('send', Adw.ResponseAppearance.SUGGESTED);
            dialog.set_default_response('cancel');
            dialog.set_close_response('cancel');
            dialog.connect('response', async (_d, response) => {
                if (response !== 'send')
                    return;
                status.visible = false;
                try {
                    const {default: Geoclue} = await import('gi://Geoclue');
                    const simple = await new Promise((resolve, reject) =>
                        Geoclue.Simple.new(APP_ID, Geoclue.AccuracyLevel.CITY, cancellable, (_o, res) => {
                            try {
                                resolve(Geoclue.Simple.new_finish(res));
                            } catch (e) {
                                reject(e);
                            }
                        }));
                    const location = simple.get_location();
                    const lat = roundForPrivacy(location.latitude);
                    const lon = roundForPrivacy(location.longitude);
                    if (lat === null || lon === null)
                        throw new Error('no coordinates');
                    choose(t.myPlace, lat, lon);
                } catch (e) {
                    console.warn(`Vilna prefs: no location: ${e.message}`);
                    status.label = t.locFailed;
                    status.visible = true;
                }
            });
            dialog.present(window);
        });

        page.add(where);

        // Weather warnings (weatherAlerts.js, lib/alerts.js). MeteoAlarm's
        // terms ask for the source, a link and their disclaimer, word for word.
        const a = alertText(GLib.get_language_names());
        const alerts = new Adw.PreferencesGroup({title: a.title, description: a.meteredHint});
        settings.bind('weather', alerts, 'sensitive', Gio.SettingsBindFlags.GET);
        const show = new Adw.SwitchRow({title: a.show});
        settings.bind('weather-alerts', show, 'active', Gio.SettingsBindFlags.DEFAULT);
        alerts.add(show);
        const metered = new Adw.SwitchRow({title: a.metered});
        settings.bind('alerts-metered', metered, 'active', Gio.SettingsBindFlags.DEFAULT);
        alerts.add(metered);
        const zone = new Adw.ActionRow({title: ''});
        zone.add_css_class('property');
        const showZone = () => {
            const [, , codes] = settings.get_value('alert-zones').deepUnpack();
            zone.title = codes.length ? a.zone.replace('%s', codes.join(', ')) : a.zoneNone;
        };
        const zoneId = settings.connect('changed::alert-zones', showZone);
        window.connect('close-request', () => {
            settings.disconnect(zoneId);
            return false;
        });
        showZone();
        alerts.add(zone);
        const source = new Adw.ActionRow({title: a.source, subtitle: DISCLAIMER, activatable: true});
        source.add_suffix(new Gtk.Image({icon_name: 'adw-external-link-symbolic'}));
        source.connect('activated', () => Gio.AppInfo.launch_default_for_uri(SITE_URL, null));
        alerts.add(source);
        page.add(alerts);

        window.add(this._soundsPage(settings));
    }

    // Sounds (sounds.js, lib/soundTheme.js): the theme, all event sounds on
    // or off (GNOME's own switch), and each sound on or off.
    _soundsPage(settings) {
        const s = soundText(GLib.get_language_names());
        const sound = new Gio.Settings({schema_id: 'org.gnome.desktop.sound'});
        const page = new Adw.PreferencesPage({title: s.page, icon_name: 'audio-speakers-symbolic'});

        const themeGroup = new Adw.PreferencesGroup({title: s.groupTheme});
        const all = new Adw.SwitchRow({title: s.all, subtitle: s.allHint});
        sound.bind('event-sounds', all, 'active', Gio.SettingsBindFlags.DEFAULT);
        themeGroup.add(all);
        const themes = new Adw.ComboRow({
            title: s.theme,
            model: Gtk.StringList.new(SOUND_THEMES.map(theme => s.themes[theme])),
        });
        const showTheme = () => {
            const i = SOUND_THEMES.indexOf(settings.get_string('sound-theme'));
            themes.selected = i < 0 ? 0 : i;
        };
        showTheme();
        themes.connect('notify::selected', () => {
            const theme = SOUND_THEMES[themes.selected];
            if (theme && theme !== settings.get_string('sound-theme'))
                settings.set_string('sound-theme', theme);
        });
        themeGroup.add(themes);
        page.add(themeGroup);

        const events = new Adw.PreferencesGroup({title: s.groupEvents, description: s.eventsHint});
        sound.bind('event-sounds', events, 'sensitive', Gio.SettingsBindFlags.GET);
        const rows = new Map();
        for (const event of SOUND_EVENTS) {
            const row = new Adw.SwitchRow({title: s.events[event]});
            const play = new Gtk.Button({
                icon_name: 'media-playback-start-symbolic', tooltip_text: s.play, valign: Gtk.Align.CENTER,
            });
            play.add_css_class('flat');
            play.connect('clicked', () => {
                // The sound as the person's theme plays it now.
                try {
                    Gio.Subprocess.new(['canberra-gtk-play', '-i', event], Gio.SubprocessFlags.NONE);
                } catch (e) {
                    logError(e, 'Vilna: cannot play the sound');
                }
            });
            row.add_prefix(play);
            row.connect('notify::active', () => {
                const off = new Set(settings.get_strv('disabled-sounds'));
                if (row.active)
                    off.delete(event);
                else
                    off.add(event);
                const next = SOUND_EVENTS.filter(e => off.has(e));
                if (next.join() !== settings.get_strv('disabled-sounds').join())
                    settings.set_strv('disabled-sounds', next);
            });
            rows.set(event, row);
            events.add(row);
        }
        const showEvents = () => {
            const off = new Set(settings.get_strv('disabled-sounds'));
            for (const [event, row] of rows)
                row.active = !off.has(event);
        };
        showEvents();
        const ids = [
            settings.connect('changed::sound-theme', showTheme),
            settings.connect('changed::disabled-sounds', showEvents),
        ];
        page.connect('destroy', () => ids.forEach(id => settings.disconnect(id)));
        page.add(events);
        return page;
    }
}

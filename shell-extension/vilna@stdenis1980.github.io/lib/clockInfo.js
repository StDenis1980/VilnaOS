// SPDX-License-Identifier: GPL-3.0-or-later
//
// (з) The clock card of the desktop widgets: the logic without GNOME, so
// that node can test it. The next alarm and the other time zones come
// from GNOME Clocks (org.gnome.clocks), which Vilna installs as a
// Flatpak: its settings live in its own keyfile, read here and never
// written, except the tab Clocks opens on (see clockState()).
//
// Clocks keeps each alarm as a dictionary: hour, minute, days (ISO week
// days, 1 Monday ... 7 Sunday; none for a one-off alarm), name and, while
// the alarm is on, ring_time, the next time it rings as ISO 8601 (a
// snoozed alarm moves it). Clocks before 3.36 wrote active instead.
// Clocks rings only while it runs: closing its window stops it.

import {languageOf} from './widgets.js';

export const FLATPAK_KEYFILE = ['.var', 'app', 'org.gnome.clocks', 'config', 'glib-2.0', 'settings', 'keyfile'];
export const CLOCKS_GROUP = 'org/gnome/clocks';
export const CLOCKS_STATE_GROUP = 'org/gnome/clocks/state/window';
export const CLOCKS_BUS_NAME = 'org.gnome.clocks';
export const CLOCKS_APP = 'org.gnome.clocks.desktop';
// The tabs of Clocks (its enum org.gnome.clocks.panelid).
export const PANELS = ['world', 'alarm', 'stopwatch', 'timer'];
export const MAX_ZONES = 2;

const DAY = 86400000;

// GLib writes ISO 8601 offsets as '+01' or '+0100'; Date wants '+01:00'.
export function parseIso(text) {
    if (typeof text !== 'string')
        return null;
    const fixed = text.trim().replace(/([+-]\d{2})(\d{2})?$/, (_, h, m) => `${h}:${m ?? '00'}`);
    const date = new Date(fixed);
    return Number.isNaN(date.getTime()) ? null : date;
}
const int = v => (typeof v === 'number' && Number.isInteger(v) ? v : null);

// alarm: one dictionary of Clocks' alarms (unpacked); now: a Date.
// Returns the Date it rings next, or null when it is off or already rang.
export function nextRing(alarm, now) {
    const hour = int(alarm?.hour);
    const minute = int(alarm?.minute);
    if (hour === null || minute === null || hour < 0 || hour > 23 || minute < 0 || minute > 59)
        return null;
    const on = typeof alarm.ring_time === 'string' || alarm.active === true;
    if (!on)
        return null;
    const ring = parseIso(alarm.ring_time);
    if (ring && ring > now)
        return ring;
    const days = new Set((Array.isArray(alarm.days) ? alarm.days : []).map(int).filter(d => d >= 1 && d <= 7));
    // A one-off alarm whose ring time has passed has rung (or was missed
    // while Clocks was closed); it rings no more.
    if (!days.size && typeof alarm.ring_time === 'string')
        return null;
    for (let i = 0; i <= 7; i++) {
        const at = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i, hour, minute);
        const isoDay = at.getDay() || 7;
        if (at > now && (!days.size || days.has(isoDay)))
            return at;
    }
    return null;
}

// alarms: Clocks' list (unpacked). Returns {at: Date, name} of the alarm
// that rings first, or null.
export function nextAlarm(alarms, now) {
    let best = null;
    for (const alarm of Array.isArray(alarms) ? alarms : []) {
        const at = nextRing(alarm, now);
        if (at && (!best || at < best.at))
            best = {at, name: typeof alarm.name === 'string' ? alarm.name.trim() : ''};
    }
    return best;
}

// 'today', 'tomorrow', 'week' (within six days: the weekday is enough) or
// 'date', counted in calendar days of the local time.
export function whenKind(at, now) {
    const day = d => Math.round(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / DAY);
    const diff = day(at) - day(now);
    if (diff <= 0)
        return 'today';
    if (diff === 1)
        return 'tomorrow';
    return diff < 7 ? 'week' : 'date';
}

// seconds: the other zone's offset minus ours. '+1', '−6', '+5:30', or ''
// when it is the same time.
export function offsetText(seconds) {
    const minutes = Math.round(Number(seconds) / 60);
    if (!Number.isFinite(minutes) || minutes === 0)
        return '';
    const abs = Math.abs(minutes);
    const h = Math.floor(abs / 60);
    const m = abs % 60;
    return `${minutes > 0 ? '+' : '−'}${h}${m ? `:${String(m).padStart(2, '0')}` : ''}`;
}

// zones: [{name, offset (s from UTC now)}] in Clocks' order; local: our
// offset. Keeps the zones whose time differs from ours, without repeats,
// at most max.
export function zoneRows(zones, local, max = MAX_ZONES) {
    const rows = [];
    const seen = new Set();
    for (const zone of Array.isArray(zones) ? zones : []) {
        const offset = Number(zone?.offset);
        const name = String(zone?.name ?? '').trim();
        if (!name || !Number.isFinite(offset) || offset === local || seen.has(name))
            continue;
        seen.add(name);
        rows.push({name, offset, diff: offsetText(offset - local)});
        if (rows.length >= max)
            break;
    }
    return rows;
}

// panel: one of PANELS. The GVariant text Clocks' keyfile holds for it.
export function clockState(panel) {
    return PANELS.includes(panel) ? `'${panel}'` : null;
}

// The words, in the ten languages of the widgets.
const KEYS = ['clock', 'today', 'tomorrow', 'hours', 'closed', 'alarms', 'world', 'stopwatch', 'timer', 'dateTime',
    'settings', 'showAlarm', 'showZones', 'menu'];

const TEXT = {
    en: ['Clock', 'today', 'tomorrow', '%s h', 'Rings only while Clocks is open', 'Alarms', 'World clocks',
        'Stopwatch', 'Timer', 'Date & time', 'Settings', 'Show the next alarm', 'Show other time zones',
        'Clock: alarms, world clocks, stopwatch, timer'],
    uk: ['Годинник', 'сьогодні', 'завтра', '%s год', 'Задзвонить, лише коли «Годинник» відкритий', 'Будильники',
        'Світовий час', 'Секундомір', 'Таймер', 'Дата й час', 'Налаштування', 'Показувати найближчий будильник',
        'Показувати інші часові пояси', 'Годинник: будильники, світовий час, секундомір, таймер'],
    es: ['Reloj', 'hoy', 'mañana', '%s h', 'Solo suena con Relojes abierto', 'Alarmas', 'Relojes del mundo',
        'Cronómetro', 'Temporizador', 'Fecha y hora', 'Configuración', 'Mostrar la próxima alarma',
        'Mostrar otras zonas horarias', 'Reloj: alarmas, relojes del mundo, cronómetro, temporizador'],
    pl: ['Zegar', 'dziś', 'jutro', '%s godz.', 'Zadzwoni tylko przy otwartym Zegarze', 'Budziki', 'Zegar światowy',
        'Stoper', 'Minutnik', 'Data i czas', 'Ustawienia', 'Pokazuj najbliższy budzik', 'Pokazuj inne strefy czasowe',
        'Zegar: budziki, zegar światowy, stoper, minutnik'],
    de: ['Uhr', 'heute', 'morgen', '%s Std.', 'Klingelt nur, solange Uhren geöffnet ist', 'Wecker', 'Weltuhren',
        'Stoppuhr', 'Timer', 'Datum & Uhrzeit', 'Einstellungen', 'Nächsten Wecker anzeigen',
        'Andere Zeitzonen anzeigen', 'Uhr: Wecker, Weltuhren, Stoppuhr, Timer'],
    fr: ['Horloge', 'aujourd’hui', 'demain', '%s h', 'Ne sonne que si Horloges est ouvert', 'Alarmes',
        'Horloges mondiales', 'Chronomètre', 'Minuteur', 'Date et heure', 'Paramètres',
        'Afficher la prochaine alarme', 'Afficher d’autres fuseaux horaires',
        'Horloge : alarmes, horloges mondiales, chronomètre, minuteur'],
    it: ['Orologio', 'oggi', 'domani', '%s h', 'Suona solo con Orologi aperto', 'Sveglie', 'Orologi del mondo',
        'Cronometro', 'Timer', 'Data e ora', 'Impostazioni', 'Mostra la prossima sveglia', 'Mostra altri fusi orari',
        'Orologio: sveglie, orologi del mondo, cronometro, timer'],
    ro: ['Ceas', 'azi', 'mâine', '%s h', 'Sună doar cât timp Ceasuri e deschis', 'Alarme', 'Ceasuri din lume',
        'Cronometru', 'Temporizator', 'Data și ora', 'Setări', 'Arată următoarea alarmă', 'Arată alte fuse orare',
        'Ceas: alarme, ceasuri din lume, cronometru, temporizator'],
    cs: ['Hodiny', 'dnes', 'zítra', '%s h', 'Zazvoní, jen když jsou Hodiny otevřené', 'Budíky', 'Světový čas',
        'Stopky', 'Minutka', 'Datum a čas', 'Nastavení', 'Zobrazit nejbližší budík', 'Zobrazit další časová pásma',
        'Hodiny: budíky, světový čas, stopky, minutka'],
    lt: ['Laikrodis', 'šiandien', 'rytoj', '%s val.', 'Skambės, tik kai Laikrodžiai atverti', 'Žadintuvai',
        'Pasaulio laikas', 'Chronometras', 'Laikmatis', 'Data ir laikas', 'Nustatymai', 'Rodyti artimiausią žadintuvą',
        'Rodyti kitas laiko juostas', 'Laikrodis: žadintuvai, pasaulio laikas, chronometras, laikmatis'],
};

export const CLOCK_LANGUAGES = Object.keys(TEXT);

export function clockText(languageNames) {
    const code = languageOf(languageNames);
    const values = TEXT[code] ?? TEXT.en;
    return Object.fromEntries(KEYS.map((key, i) => [key, values[i]]));
}

export function checkClockTables() {
    return CLOCK_LANGUAGES.filter(code => TEXT[code].length !== KEYS.length);
}

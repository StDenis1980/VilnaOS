// SPDX-License-Identifier: GPL-3.0-or-later
//
// (з) Desktop widgets: the logic without GNOME, so that node can test it.
// Which place the weather is for, the Open-Meteo requests and answers,
// the line about Копії (vilna-backup), the command that hands a question
// to Gloxi, and the words in the ten languages of Vilna (ADR 0009).
//
// Privacy: by default the weather is for the capital of the country the
// system is set to (its time zone, then its region), so no location of
// the person leaves the computer. Only when the person changes the place
// does something else go out: the text they type into the search, or,
// after they agree, their approximate position rounded to about 10 km.

export const FORECAST_URL = 'https://api.open-meteo.com/v1/forecast';
export const GEOCODE_URL = 'https://geocoding-api.open-meteo.com/v1/search';
// Coordinates sent for "my location" keep one decimal: about 11 km.
export const LOCATION_DECIMALS = 1;
export const REFRESH_MINUTES = 30;
export const RETRY_MINUTES = 10;
export const MAX_EVENTS = 3;
export const MAX_RESULTS = 8;

// Capitals: the country code of zone.tab, the capital's own name, and its
// coordinates (two decimals, public geographic facts).
export const CAPITALS = {
    AD: ['Andorra la Vella', 42.51, 1.52], AL: ['Tiranë', 41.33, 19.82], AM: ['Երևան', 40.18, 44.51],
    AT: ['Wien', 48.21, 16.37], AZ: ['Bakı', 40.41, 49.87], BA: ['Sarajevo', 43.86, 18.41],
    BE: ['Bruxelles', 50.85, 4.35], BG: ['София', 42.70, 23.32], BY: ['Мінск', 53.90, 27.57],
    CA: ['Ottawa', 45.42, -75.70], CH: ['Bern', 46.95, 7.45], CY: ['Λευκωσία', 35.17, 33.36],
    CZ: ['Praha', 50.08, 14.44], DE: ['Berlin', 52.52, 13.40], DK: ['København', 55.68, 12.57],
    EE: ['Tallinn', 59.44, 24.75], ES: ['Madrid', 40.42, -3.70], FI: ['Helsinki', 60.17, 24.94],
    FR: ['Paris', 48.86, 2.35], GB: ['London', 51.51, -0.13], GE: ['თბილისი', 41.72, 44.79],
    GR: ['Αθήνα', 37.98, 23.73], HR: ['Zagreb', 45.81, 15.98], HU: ['Budapest', 47.50, 19.04],
    IE: ['Dublin', 53.35, -6.26], IL: ['ירושלים', 31.77, 35.21], IS: ['Reykjavík', 64.15, -21.94],
    IT: ['Roma', 41.90, 12.50], KZ: ['Астана', 51.17, 71.45], LI: ['Vaduz', 47.14, 9.52],
    LT: ['Vilnius', 54.69, 25.28], LU: ['Luxembourg', 49.61, 6.13], LV: ['Rīga', 56.95, 24.11],
    MC: ['Monaco', 43.73, 7.42], MD: ['Chișinău', 47.01, 28.86], ME: ['Podgorica', 42.44, 19.26],
    MK: ['Скопје', 42.00, 21.43], MT: ['Valletta', 35.90, 14.51], NL: ['Amsterdam', 52.37, 4.90],
    NO: ['Oslo', 59.91, 10.75], PL: ['Warszawa', 52.23, 21.01], PT: ['Lisboa', 38.72, -9.14],
    RO: ['București', 44.43, 26.10], RS: ['Београд', 44.79, 20.45], SE: ['Stockholm', 59.33, 18.07],
    SI: ['Ljubljana', 46.06, 14.51], SK: ['Bratislava', 48.15, 17.11], SM: ['San Marino', 43.94, 12.45],
    TR: ['Ankara', 39.93, 32.86], UA: ['Київ', 50.45, 30.52], US: ['Washington', 38.91, -77.04],
    VA: ['Città del Vaticano', 41.90, 12.45], AU: ['Canberra', -35.28, 149.13], BR: ['Brasília', -15.79, -47.88],
    AR: ['Buenos Aires', -34.60, -58.38], MX: ['Ciudad de México', 19.43, -99.13], JP: ['東京', 35.68, 139.69],
    MA: ['الرباط', 34.02, -6.84], NZ: ['Wellington', -41.29, 174.78], CL: ['Santiago', -33.45, -70.67],
    CO: ['Bogotá', 4.71, -74.07], PE: ['Lima', -12.05, -77.04], ZA: ['Pretoria', -25.75, 28.19],
};

const finite = v => typeof v === 'number' && Number.isFinite(v);

// zoneTab: the text of /usr/share/zoneinfo/zone.tab. Returns 'ES' for
// 'Europe/Madrid', or null.
export function countryOfZone(zone, zoneTab) {
    if (!zone || typeof zoneTab !== 'string')
        return null;
    for (const line of zoneTab.split('\n')) {
        if (line.startsWith('#'))
            continue;
        const [code, , tz] = line.split('\t');
        if (tz === zone && /^[A-Z]{2}$/.test(code ?? ''))
            return code;
    }
    return null;
}

// 'uk_UA.UTF-8' -> 'UA'; '' or 'C' -> null.
export function countryOfLocale(locale) {
    const m = /^[a-z]{2,3}_([A-Z]{2})\b/.exec(String(locale ?? ''));
    return m ? m[1] : null;
}

// The country the default weather place comes from: where the clock is
// set (someone from Ukraine living in Spain gets Madrid), then the
// region format, then the language.
export function homeCountry({zone, zoneTab, region, lang} = {}) {
    for (const code of [countryOfZone(zone, zoneTab), countryOfLocale(region), countryOfLocale(lang)]) {
        if (code && CAPITALS[code])
            return code;
    }
    return null;
}

// The place the weather is for: the person's choice, else the capital.
// chosen: [name, latitude, longitude] from the setting weather-location.
export function weatherPlace(chosen, country) {
    const [name, lat, lon] = Array.isArray(chosen) ? chosen : [];
    if (typeof name === 'string' && name && finite(lat) && finite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180)
        return {name, lat, lon, capital: false};
    const capital = CAPITALS[country];
    if (!capital)
        return null;
    return {name: capital[0], lat: capital[1], lon: capital[2], capital: true};
}

export function roundForPrivacy(value) {
    const f = 10 ** LOCATION_DECIMALS;
    return finite(value) ? Math.round(value * f) / f : null;
}

export function forecastUrl(place, base = FORECAST_URL) {
    const q = [
        `latitude=${place.lat.toFixed(2)}`, `longitude=${place.lon.toFixed(2)}`,
        'current=temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m,wind_gusts_10m',
        'daily=temperature_2m_max,temperature_2m_min',
        'timezone=auto', 'forecast_days=1',
    ];
    return `${base}?${q.join('&')}`;
}

// The Open-Meteo answer -> {temp, high, low, code, day, feels, wind, gusts}
// (wind in km/h; feels, wind and gusts null when missing), or null.
export function parseForecast(text) {
    let data;
    try {
        data = JSON.parse(text);
    } catch {
        return null;
    }
    const current = data?.current ?? {};
    const daily = data?.daily ?? {};
    const temp = current.temperature_2m;
    const code = current.weather_code;
    if (!finite(temp) || !Number.isInteger(code))
        return null;
    const first = a => Array.isArray(a) && finite(a[0]) ? Math.round(a[0]) : null;
    const value = v => finite(v) ? Math.round(v) : null;
    return {temp: Math.round(temp), high: first(daily.temperature_2m_max), low: first(daily.temperature_2m_min),
        code, day: current.is_day !== 0, feels: value(current.apparent_temperature),
        wind: value(current.wind_speed_10m), gusts: value(current.wind_gusts_10m)};
}

export function geocodeUrl(query, language, base = GEOCODE_URL) {
    const lang = /^[a-z]{2}$/.test(language ?? '') ? language : 'en';
    return `${base}?name=${encodeURIComponent(String(query).trim().slice(0, 100))}&count=${MAX_RESULTS}&language=${lang}&format=json`;
}

// The geocoding answer -> [{name, detail, lat, lon}].
export function parseGeocode(text) {
    let data;
    try {
        data = JSON.parse(text);
    } catch {
        return [];
    }
    const results = Array.isArray(data?.results) ? data.results : [];
    return results.slice(0, MAX_RESULTS).flatMap(r => {
        if (typeof r?.name !== 'string' || !finite(r.latitude) || !finite(r.longitude))
            return [];
        const detail = [r.admin1, r.country].filter(s => typeof s === 'string' && s && s !== r.name).join(', ');
        return [{name: r.name, detail, lat: r.latitude, lon: r.longitude}];
    });
}

// WMO weather codes -> one of eight kinds, each with an Adwaita icon. Two
// more come from the wind and the heat (weatherScene).
const KINDS = [
    ['clear', 'weather-clear-symbolic', 'weather-clear-night-symbolic'],
    ['cloudy', 'weather-few-clouds-symbolic', 'weather-few-clouds-night-symbolic'],
    ['overcast', 'weather-overcast-symbolic'],
    ['fog', 'weather-fog-symbolic'],
    ['drizzle', 'weather-showers-scattered-symbolic'],
    ['rain', 'weather-showers-symbolic'],
    ['snow', 'weather-snow-symbolic'],
    ['storm', 'weather-storm-symbolic'],
    ['wind', 'weather-windy-symbolic'],
    ['heat', 'weather-clear-symbolic', 'weather-clear-night-symbolic'],
];

// Above these, a dry sky becomes windy (km/h) or hot (°C, as it feels).
// 50 km/h gusts are where Spain's yellow wind warnings begin inland;
// 35 °C is a hot day almost anywhere in Europe.
export const WINDY_GUSTS = 50;
export const WINDY_WIND = 30;
export const HOT = 35;

export function weatherKind(code, day = true) {
    let i;
    if (code === 0) i = 0;
    else if (code === 1 || code === 2) i = 1;
    else if (code === 3) i = 2;
    else if (code === 45 || code === 48) i = 3;
    else if (code >= 51 && code <= 57) i = 4;
    else if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) i = 5;
    else if ((code >= 71 && code <= 77) || code === 85 || code === 86) i = 6;
    else if (code >= 95 && code <= 99) i = 7;
    else i = 2;
    const [kind, icon, night] = KINDS[i];
    return {kind, index: i, icon: !day && night ? night : icon};
}

// The forecast -> what the card shows and draws: a weatherKind, where
// a clear or cloudy sky may be hot instead, and a dry sky windy.
export function weatherScene(forecast) {
    if (!forecast)
        return null;
    const base = weatherKind(forecast.code, forecast.day);
    const pick = i => ({kind: KINDS[i][0], index: i, icon: !forecast.day && KINDS[i][2] ? KINDS[i][2] : KINDS[i][1]});
    const dry = base.index <= 2;
    const feels = forecast.feels ?? forecast.temp;
    if (dry && base.index <= 1 && finite(feels) && feels >= HOT)
        return {...pick(9), day: forecast.day};
    if (dry && ((forecast.gusts ?? 0) >= WINDY_GUSTS || (forecast.wind ?? 0) >= WINDY_WIND))
        return {...pick(8), day: forecast.day};
    return {...base, day: forecast.day};
}

// runsJsonl: the text of ~/.local/state/vilna-backup/runs.jsonl. Returns
// the last backup run as {time (s), ok}, or null when there is none.
export function lastBackup(runsJsonl) {
    const lines = String(runsJsonl ?? '').split('\n');
    for (let i = lines.length - 1; i >= 0; i--) {
        let rec;
        try {
            rec = JSON.parse(lines[i]);
        } catch {
            continue;
        }
        if (rec?.kind === 'backup' && finite(rec.time))
            return {time: rec.time, ok: rec.ok === true};
    }
    return null;
}

// When the last copy was, said as today / yesterday / a date. now and
// time are seconds; offset is the local offset from UTC in seconds;
// clock formats a time as HH:MM, date formats a day.
export function backupWhen(time, now, offset, clock, date) {
    const day = s => Math.floor((s + offset) / 86400);
    const diff = day(now) - day(time);
    if (diff === 0)
        return ['today', clock(time)];
    if (diff === 1)
        return ['yesterday', clock(time)];
    return ['on', date(time)];
}

// commandline: the Exec line of Gloxi's .desktop file (for the Flatpak,
// 'flatpak run ... io.github.stdenis1980.Gloxi'); argv: GLib.shell_parse_argv
// of it. Returns the argv that asks Gloxi the question.
export function askArgv(argv, question) {
    const text = String(question ?? '').trim();
    if (!Array.isArray(argv) || !argv.length || !text)
        return null;
    return [...argv.filter(a => !/^%[a-zA-Z]$/.test(a)), '--ask', text];
}

// The words, in the ten languages of Vilna. The extension has no gettext
// catalogue of its own.
const KEYS = ['gloxi', 'help', 'askPlaceholder', 'chips', 'today', 'nothing', 'allDay', 'weather', 'noWeather',
    'chooseCity', 'kinds', 'backupOk', 'backupFailed', 'backupNone', 'last', 'prefsTitle', 'showWidgets',
    'showWeather', 'place', 'capital', 'search', 'searchHint', 'useMine', 'consentTitle', 'consentBody', 'send',
    'cancel', 'backToCapital', 'notFound', 'locFailed', 'myPlace', 'dataBy'];

const TEXT = {
    en: ['Gloxi', 'How can I help?', 'Ask Gloxi…', ['What’s on today?', 'Find a file', 'Set up Wi-Fi'],
        'Today · Calendar', 'Nothing planned', 'All day', 'Weather', 'No forecast yet', 'Choose a town',
        ['Clear', 'Partly cloudy', 'Overcast', 'Fog', 'Drizzle', 'Rain', 'Snow', 'Thunderstorm', 'Windy', 'Hot'],
        'Backups: all saved', 'Backups: the last one failed', 'Backups are not set up',
        {today: 'Last: today at %s', yesterday: 'Last: yesterday at %s', on: 'Last: %s'},
        'Desktop widgets', 'Show the widgets', 'Show the weather', 'Place for the weather', '%s (the capital, by default)',
        'Search for a town', 'Only the text you type is sent to Open-Meteo.', 'Use my location', 'Send your location?',
        'Vilna will ask the system for your approximate location, rounded to about 10 km, and send it to Open-Meteo for the forecast. Nothing else is sent.',
        'Send', 'Cancel', 'Back to the capital', 'Nothing found', 'Could not find your location. Type a town instead.',
        'My location', 'Weather data: Open-Meteo.com (CC BY 4.0)'],
    uk: ['Gloxi', 'Чим допомогти?', 'Запитайте Gloxi…', ['Що сьогодні?', 'Знайди файл', 'Налаштуй Wi-Fi'],
        'Сьогодні · календар', 'Нічого не заплановано', 'Увесь день', 'Погода', 'Прогнозу ще немає', 'Оберіть місто',
        ['Ясно', 'Мінлива хмарність', 'Хмарно', 'Туман', 'Мряка', 'Дощ', 'Сніг', 'Гроза', 'Вітряно', 'Спека'],
        'Копії: усе збережено', 'Копії: остання не вдалася', 'Копії ще не налаштовано',
        {today: 'Остання сьогодні о %s', yesterday: 'Остання вчора о %s', on: 'Остання %s'},
        'Віджети стільниці', 'Показувати віджети', 'Показувати погоду', 'Місце для погоди', '%s (столиця, за замовчуванням)',
        'Пошук міста', 'В Open-Meteo йде лише текст, який ви вводите.', 'Моє розташування', 'Передати розташування?',
        'Vilna попросить у системи ваше приблизне розташування, округлене до близько 10 км, і передасть його Open-Meteo для прогнозу. Більше нічого не передається.',
        'Передати', 'Скасувати', 'Повернути столицю', 'Нічого не знайдено', 'Не вдалося визначити розташування. Введіть місто.',
        'Моє розташування', 'Дані погоди: Open-Meteo.com (CC BY 4.0)'],
    es: ['Gloxi', '¿En qué te ayudo?', 'Pregunta a Gloxi…', ['¿Qué hay hoy?', 'Busca un archivo', 'Configura el wifi'],
        'Hoy · Calendario', 'Nada previsto', 'Todo el día', 'Tiempo', 'Aún no hay previsión', 'Elige una ciudad',
        ['Despejado', 'Parcialmente nublado', 'Nublado', 'Niebla', 'Llovizna', 'Lluvia', 'Nieve', 'Tormenta', 'Viento', 'Calor'],
        'Copias: todo guardado', 'Copias: la última falló', 'Las copias no están configuradas',
        {today: 'Última: hoy a las %s', yesterday: 'Última: ayer a las %s', on: 'Última: %s'},
        'Widgets del escritorio', 'Mostrar los widgets', 'Mostrar el tiempo', 'Lugar para el tiempo', '%s (la capital, por defecto)',
        'Buscar una ciudad', 'Solo se envía a Open-Meteo el texto que escribes.', 'Usar mi ubicación', '¿Enviar tu ubicación?',
        'Vilna pedirá al sistema tu ubicación aproximada, redondeada a unos 10 km, y la enviará a Open-Meteo para la previsión. No se envía nada más.',
        'Enviar', 'Cancelar', 'Volver a la capital', 'No se encontró nada', 'No se pudo encontrar tu ubicación. Escribe una ciudad.',
        'Mi ubicación', 'Datos del tiempo: Open-Meteo.com (CC BY 4.0)'],
    pl: ['Gloxi', 'W czym pomóc?', 'Zapytaj Gloxi…', ['Co dziś?', 'Znajdź plik', 'Skonfiguruj Wi-Fi'],
        'Dziś · kalendarz', 'Nic nie zaplanowano', 'Cały dzień', 'Pogoda', 'Brak prognozy', 'Wybierz miasto',
        ['Bezchmurnie', 'Częściowe zachmurzenie', 'Pochmurno', 'Mgła', 'Mżawka', 'Deszcz', 'Śnieg', 'Burza', 'Wietrznie', 'Upał'],
        'Kopie: wszystko zapisane', 'Kopie: ostatnia się nie udała', 'Kopie nie są skonfigurowane',
        {today: 'Ostatnia: dziś o %s', yesterday: 'Ostatnia: wczoraj o %s', on: 'Ostatnia: %s'},
        'Widżety pulpitu', 'Pokazuj widżety', 'Pokazuj pogodę', 'Miejsce dla pogody', '%s (stolica, domyślnie)',
        'Szukaj miasta', 'Do Open-Meteo trafia tylko wpisany tekst.', 'Użyj mojej lokalizacji', 'Wysłać lokalizację?',
        'Vilna poprosi system o przybliżoną lokalizację, zaokrągloną do około 10 km, i wyśle ją do Open-Meteo po prognozę. Nic więcej nie jest wysyłane.',
        'Wyślij', 'Anuluj', 'Wróć do stolicy', 'Nic nie znaleziono', 'Nie udało się ustalić lokalizacji. Wpisz miasto.',
        'Moja lokalizacja', 'Dane pogodowe: Open-Meteo.com (CC BY 4.0)'],
    de: ['Gloxi', 'Wobei kann ich helfen?', 'Gloxi fragen …', ['Was steht heute an?', 'Datei finden', 'WLAN einrichten'],
        'Heute · Kalender', 'Nichts geplant', 'Ganztägig', 'Wetter', 'Noch keine Vorhersage', 'Ort wählen',
        ['Klar', 'Teils bewölkt', 'Bedeckt', 'Nebel', 'Nieselregen', 'Regen', 'Schnee', 'Gewitter', 'Windig', 'Hitze'],
        'Sicherungen: alles gesichert', 'Sicherungen: die letzte ist fehlgeschlagen', 'Sicherungen sind nicht eingerichtet',
        {today: 'Zuletzt: heute um %s', yesterday: 'Zuletzt: gestern um %s', on: 'Zuletzt: %s'},
        'Schreibtisch-Widgets', 'Widgets anzeigen', 'Wetter anzeigen', 'Ort für das Wetter', '%s (Hauptstadt, Standard)',
        'Ort suchen', 'An Open-Meteo geht nur der eingegebene Text.', 'Meinen Standort verwenden', 'Standort senden?',
        'Vilna fragt das System nach Ihrem ungefähren Standort, auf etwa 10 km gerundet, und sendet ihn für die Vorhersage an Open-Meteo. Sonst wird nichts gesendet.',
        'Senden', 'Abbrechen', 'Zurück zur Hauptstadt', 'Nichts gefunden', 'Standort nicht gefunden. Bitte einen Ort eingeben.',
        'Mein Standort', 'Wetterdaten: Open-Meteo.com (CC BY 4.0)'],
    fr: ['Gloxi', 'Comment puis-je aider ?', 'Demander à Gloxi…', ['Quoi de prévu ?', 'Trouver un fichier', 'Configurer le Wi-Fi'],
        'Aujourd’hui · Agenda', 'Rien de prévu', 'Toute la journée', 'Météo', 'Pas encore de prévisions', 'Choisir une ville',
        ['Dégagé', 'Partiellement nuageux', 'Couvert', 'Brouillard', 'Bruine', 'Pluie', 'Neige', 'Orage', 'Venteux', 'Chaleur'],
        'Sauvegardes : tout est enregistré', 'Sauvegardes : la dernière a échoué', 'Les sauvegardes ne sont pas configurées',
        {today: 'Dernière : aujourd’hui à %s', yesterday: 'Dernière : hier à %s', on: 'Dernière : %s'},
        'Widgets du bureau', 'Afficher les widgets', 'Afficher la météo', 'Lieu pour la météo', '%s (la capitale, par défaut)',
        'Chercher une ville', 'Seul le texte saisi est envoyé à Open-Meteo.', 'Utiliser ma position', 'Envoyer votre position ?',
        'Vilna demandera au système votre position approximative, arrondie à environ 10 km, et l’enverra à Open-Meteo pour les prévisions. Rien d’autre n’est envoyé.',
        'Envoyer', 'Annuler', 'Revenir à la capitale', 'Aucun résultat', 'Position introuvable. Saisissez une ville.',
        'Ma position', 'Données météo : Open-Meteo.com (CC BY 4.0)'],
    it: ['Gloxi', 'Come posso aiutarti?', 'Chiedi a Gloxi…', ['Cosa c’è oggi?', 'Trova un file', 'Configura il Wi-Fi'],
        'Oggi · Calendario', 'Niente in programma', 'Tutto il giorno', 'Meteo', 'Nessuna previsione', 'Scegli una città',
        ['Sereno', 'Parzialmente nuvoloso', 'Coperto', 'Nebbia', 'Pioviggine', 'Pioggia', 'Neve', 'Temporale', 'Ventoso', 'Caldo'],
        'Copie: tutto salvato', 'Copie: l’ultima non è riuscita', 'Le copie non sono configurate',
        {today: 'Ultima: oggi alle %s', yesterday: 'Ultima: ieri alle %s', on: 'Ultima: %s'},
        'Widget del desktop', 'Mostra i widget', 'Mostra il meteo', 'Luogo per il meteo', '%s (la capitale, predefinita)',
        'Cerca una città', 'A Open-Meteo va solo il testo che scrivi.', 'Usa la mia posizione', 'Inviare la posizione?',
        'Vilna chiederà al sistema la tua posizione approssimativa, arrotondata a circa 10 km, e la invierà a Open-Meteo per le previsioni. Non viene inviato altro.',
        'Invia', 'Annulla', 'Torna alla capitale', 'Nessun risultato', 'Posizione non trovata. Scrivi una città.',
        'La mia posizione', 'Dati meteo: Open-Meteo.com (CC BY 4.0)'],
    ro: ['Gloxi', 'Cu ce te pot ajuta?', 'Întreabă-l pe Gloxi…', ['Ce e azi?', 'Găsește un fișier', 'Configurează Wi-Fi'],
        'Azi · Calendar', 'Nimic planificat', 'Toată ziua', 'Vremea', 'Încă nu există prognoză', 'Alege un oraș',
        ['Senin', 'Parțial noros', 'Înnorat', 'Ceață', 'Burniță', 'Ploaie', 'Ninsoare', 'Furtună', 'Vânt', 'Caniculă'],
        'Copii: totul salvat', 'Copii: ultima nu a reușit', 'Copiile nu sunt configurate',
        {today: 'Ultima: azi la %s', yesterday: 'Ultima: ieri la %s', on: 'Ultima: %s'},
        'Widgeturi pe desktop', 'Arată widgeturile', 'Arată vremea', 'Locul pentru vreme', '%s (capitala, implicit)',
        'Caută un oraș', 'La Open-Meteo ajunge doar textul pe care îl scrii.', 'Folosește locația mea', 'Trimiți locația?',
        'Vilna va cere sistemului locația ta aproximativă, rotunjită la circa 10 km, și o va trimite la Open-Meteo pentru prognoză. Nu se trimite nimic altceva.',
        'Trimite', 'Anulează', 'Înapoi la capitală', 'Nu s-a găsit nimic', 'Locația nu a putut fi găsită. Scrie un oraș.',
        'Locația mea', 'Date meteo: Open-Meteo.com (CC BY 4.0)'],
    cs: ['Gloxi', 'S čím pomoci?', 'Zeptejte se Gloxi…', ['Co je dnes?', 'Najdi soubor', 'Nastav Wi-Fi'],
        'Dnes · kalendář', 'Nic naplánováno', 'Celý den', 'Počasí', 'Zatím bez předpovědi', 'Vyberte město',
        ['Jasno', 'Polojasno', 'Zataženo', 'Mlha', 'Mrholení', 'Déšť', 'Sníh', 'Bouřka', 'Větrno', 'Vedro'],
        'Zálohy: vše uloženo', 'Zálohy: poslední se nezdařila', 'Zálohy nejsou nastavené',
        {today: 'Poslední: dnes v %s', yesterday: 'Poslední: včera v %s', on: 'Poslední: %s'},
        'Widgety plochy', 'Zobrazit widgety', 'Zobrazit počasí', 'Místo pro počasí', '%s (hlavní město, výchozí)',
        'Hledat město', 'Do Open-Meteo jde jen text, který napíšete.', 'Použít mou polohu', 'Odeslat polohu?',
        'Vilna požádá systém o vaši přibližnou polohu zaokrouhlenou asi na 10 km a pošle ji Open-Meteo kvůli předpovědi. Nic jiného se neposílá.',
        'Odeslat', 'Zrušit', 'Zpět na hlavní město', 'Nic nenalezeno', 'Polohu se nepodařilo zjistit. Napište město.',
        'Moje poloha', 'Data o počasí: Open-Meteo.com (CC BY 4.0)'],
    lt: ['Gloxi', 'Kuo galiu padėti?', 'Klauskite Gloxi…', ['Kas šiandien?', 'Rask failą', 'Nustatyk „Wi-Fi“'],
        'Šiandien · kalendorius', 'Nieko nesuplanuota', 'Visą dieną', 'Orai', 'Prognozės dar nėra', 'Pasirinkite miestą',
        ['Giedra', 'Nepastovus debesuotumas', 'Debesuota', 'Rūkas', 'Dulksna', 'Lietus', 'Sniegas', 'Perkūnija', 'Vėjuota', 'Karšta'],
        'Kopijos: viskas išsaugota', 'Kopijos: paskutinė nepavyko', 'Kopijos nenustatytos',
        {today: 'Paskutinė: šiandien %s', yesterday: 'Paskutinė: vakar %s', on: 'Paskutinė: %s'},
        'Darbalaukio valdikliai', 'Rodyti valdiklius', 'Rodyti orus', 'Vieta orams', '%s (sostinė, numatytoji)',
        'Ieškoti miesto', 'Į Open-Meteo siunčiamas tik jūsų įvestas tekstas.', 'Naudoti mano vietą', 'Siųsti vietą?',
        'Vilna paprašys sistemos apytikslės jūsų vietos, suapvalintos iki maždaug 10 km, ir nusiųs ją Open-Meteo prognozei. Daugiau nieko nesiunčiama.',
        'Siųsti', 'Atšaukti', 'Grįžti į sostinę', 'Nieko nerasta', 'Nepavyko nustatyti vietos. Įveskite miestą.',
        'Mano vieta', 'Orų duomenys: Open-Meteo.com (CC BY 4.0)'],
};

export const LANGUAGES = Object.keys(TEXT);

export function languageOf(languageNames) {
    for (const name of languageNames ?? []) {
        const code = String(name).split(/[_.@-]/)[0].toLowerCase();
        if (TEXT[code])
            return code;
    }
    return 'en';
}

// languageNames: GLib.get_language_names(), most wanted first.
export function widgetText(languageNames) {
    const values = TEXT[languageOf(languageNames)];
    return Object.fromEntries(KEYS.map((key, i) => [key, values[i]]));
}

export function checkTables() {
    return LANGUAGES.filter(code => TEXT[code].length !== KEYS.length);
}

// events: GNOME Shell calendar events ({date, end: Date, summary}) for
// today; now: a Date. Returns what is still ahead or going on today, at
// most MAX_EVENTS, as {summary, start, end, allDay}.
export function todayEvents(events, now, max = MAX_EVENTS) {
    const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return (events ?? [])
        .filter(e => e?.date instanceof Date && e?.end instanceof Date && e.end > now)
        .map(e => ({
            summary: String(e.summary ?? '').trim() || '…',
            start: e.date,
            end: e.end,
            allDay: e.date <= dayStart && e.end - e.date >= 86400000 - 1000,
        }))
        .sort((a, b) => Number(b.allDay) - Number(a.allDay) || a.start - b.start)
        .slice(0, Math.max(0, max));
}

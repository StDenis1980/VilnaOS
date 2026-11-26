// SPDX-License-Identifier: GPL-3.0-or-later
//
// Weather warnings from MeteoAlarm (EUMETNET) on the weather card:
// pure logic, tested on node. widgets.js fetches, alertZones.js finds
// the zone; this reads, matches and decides.
//
// Privacy: nothing about the person's place leaves the computer. The
// whole country's feed is fetched and matched here, against the zones
// of the weather place (lib/widgets.js weatherPlace).
//
// Terms (decision of Denys, 18.11.2026; meteoalarm.org Terms and
// Conditions §5, last updated 15/03/2024): warnings are used on terms
// equivalent to CC BY 4.0 with these requirements, each met here:
//   - shown with the unchanged original next to our short label
//     (alertOriginal);
//   - the national service named (senderName from the warning itself,
//     e.g. AEMET) and the time of issue shown (sent);
//   - a link to www.meteoalarm.org (the band opens it);
//   - fetched often enough for a delay under 5 minutes on average and
//     never over 10 (REFRESH_MINUTES);
//   - their disclaimer published (prefs.js, DISCLAIMER).

export const FEED_URL = 'https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-';
export const SITE_URL = 'https://meteoalarm.org/en/live/';
// A fetch every 10 minutes keeps the delay under 5 on average and never
// over 10, as the terms ask.
export const REFRESH_MINUTES = 10;
// The feed of a whole country is 15 KB (Sweden) to 450 KB (Ukraine);
// anything far larger is not a feed.
export const MAX_FEED_BYTES = 8 * 1024 * 1024;
const MAX_ENTRIES = 5000;
const MAX_POLYGON_POINTS = 20000;
// A warning that starts later than this is not shown yet.
export const AHEAD_HOURS = 24;

export const DISCLAIMER = 'Time delays between this website and the www.meteoalarm.org website are possible. ' +
    'For the most up-to-date awareness information as published by the participating National Meteorological ' +
    'and Hydrological Services, please refer to www.meteoalarm.org.';

// ISO country -> the slug of its MeteoAlarm feed (feeds.meteoalarm.org).
export const COUNTRY_FEEDS = {
    AD: 'andorra', AT: 'austria', BE: 'belgium', BA: 'bosnia-herzegovina', BG: 'bulgaria', HR: 'croatia',
    CY: 'cyprus', CZ: 'czechia', DK: 'denmark', EE: 'estonia', FI: 'finland', FR: 'france', DE: 'germany',
    GR: 'greece', HU: 'hungary', IS: 'iceland', IE: 'ireland', IL: 'israel', IT: 'italy', LV: 'latvia',
    LT: 'lithuania', LU: 'luxembourg', MT: 'malta', MD: 'moldova', ME: 'montenegro', NL: 'netherlands',
    NO: 'norway', PL: 'poland', PT: 'portugal', MK: 'republic-of-north-macedonia', RO: 'romania', RS: 'serbia',
    SK: 'slovakia', SI: 'slovenia', ES: 'spain', SE: 'sweden', CH: 'switzerland', UA: 'ukraine',
    GB: 'united-kingdom',
};

export function feedUrl(country) {
    const slug = COUNTRY_FEEDS[country];
    return slug ? FEED_URL + slug : null;
}

// Awareness levels by the colour MeteoAlarm gives them. The CAP
// severity is not used: services fill it in differently.
export const LEVELS = {yellow: 2, orange: 3, red: 4};
// Hazards as the feed titles name them, in the order of the words.
export const HAZARDS = ['wind', 'snow-ice', 'thunderstorm', 'fog', 'high-temperature', 'low-temperature',
    'coastalevent', 'forest-fire', 'avalanches', 'rain', 'flooding', 'rain-flood', 'marine-hazard', 'drought'];

const finite = v => typeof v === 'number' && Number.isFinite(v);

function decode(text) {
    return String(text ?? '').replace(/&(amp|lt|gt|quot|apos|#(\d{1,7})|#x([0-9a-fA-F]{1,6}));/g, (m, name, dec, hex) => {
        if (dec || hex) {
            const code = dec ? Number(dec) : parseInt(hex, 16);
            return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : '';
        }
        return {amp: '&', lt: '<', gt: '>', quot: '"', apos: "'"}[name];
    }).trim();
}

const tag = (xml, name) => {
    const m = xml.match(new RegExp(`<${name}>([\\s\\S]*?)</${name}>`));
    return m ? decode(m[1]) : null;
};

function time(text) {
    const t = Date.parse(text ?? '');
    return Number.isFinite(t) ? t : null;
}

// "lat,lon lat,lon ..." -> [[lat, lon], ...], or null when malformed.
export function parsePolygon(text) {
    const points = [];
    for (const pair of String(text ?? '').trim().split(/\s+/)) {
        if (points.length >= MAX_POLYGON_POINTS)
            return null;
        const [lat, lon] = pair.split(',').map(Number);
        if (!finite(lat) || !finite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180)
            return null;
        points.push([lat, lon]);
    }
    return points.length >= 3 ? points : null;
}

// The country's Atom feed -> the warnings in it:
// {id, title, colour, level, hazard, zones: [EMMA_ID], polygons: [[[lat, lon]]], area, event,
//  sent, onset, expires (ms), capUrl, zoneUrl}. Unreadable entries are skipped.
export function parseFeed(text) {
    const xml = String(text ?? '');
    if (xml.length > MAX_FEED_BYTES)
        return [];
    const out = [];
    for (const chunk of xml.split('<entry>').slice(1, MAX_ENTRIES + 1)) {
        const entry = chunk.split('</entry>')[0];
        const title = tag(entry, 'title') ?? '';
        const m = title.match(/^(Yellow|Orange|Red)\s+([A-Za-z-]+)\s+Warning/i);
        if (!m)
            continue;
        const colour = m[1].toLowerCase();
        const hazard = m[2].toLowerCase();
        const zones = [...entry.matchAll(/<valueName>EMMA_ID<\/valueName>\s*<value>([^<]{1,20})<\/value>/g)]
            .map(z => decode(z[1]));
        const polygons = [...entry.matchAll(/<cap:polygon>([^<]*)<\/cap:polygon>/g)]
            .map(p => parsePolygon(p[1])).filter(Boolean);
        const status = tag(entry, 'cap:status');
        const type = tag(entry, 'cap:message_type');
        if ((status && status !== 'Actual') || type === 'Cancel')
            continue;
        const capUrl = entry.match(/<link type="application\/cap\+xml" href="(https:\/\/feeds\.meteoalarm\.org\/[^"]{1,300})"/)?.[1];
        const zoneUrl = entry.match(/<link title="[^"]*" href="(https:\/\/meteoalarm\.org\?geocode=[^"]{1,80})"/)?.[1];
        const expires = time(tag(entry, 'cap:expires'));
        if (!expires || (!zones.length && !polygons.length))
            continue;
        out.push({
            id: tag(entry, 'cap:identifier') ?? tag(entry, 'id') ?? title,
            title,
            colour,
            level: LEVELS[colour],
            hazard: HAZARDS.includes(hazard) ? hazard : 'other',
            zones,
            polygons,
            area: tag(entry, 'cap:areaDesc') ?? '',
            event: tag(entry, 'cap:event') ?? '',
            sent: time(tag(entry, 'cap:sent')),
            onset: time(tag(entry, 'cap:onset')) ?? time(tag(entry, 'cap:effective')),
            expires,
            capUrl: capUrl ? decode(capUrl) : null,
            zoneUrl: zoneUrl ? decode(zoneUrl) : null,
        });
    }
    return out;
}

// Ray casting on [lat, lon] rings.
export function inRing(lat, lon, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const [yi, xi] = ring[i];
        const [yj, xj] = ring[j];
        if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi)
            inside = !inside;
    }
    return inside;
}

// The warnings for a place now: in one of its zones or around it, not
// over, starting within AHEAD_HOURS; the worst first, then the soonest.
// One per hazard and level (a zone can carry the same warning twice,
// in two languages or for two areas).
export function alertsFor(warnings, place, zones, now) {
    if (!place || !finite(place.lat) || !finite(place.lon))
        return [];
    const mine = new Set(zones ?? []);
    const ahead = now + AHEAD_HOURS * 3600000;
    const seen = new Set();
    return (warnings ?? [])
        .filter(w => w.expires > now && (w.onset ?? now) <= ahead)
        .filter(w => w.zones.some(z => mine.has(z)) || w.polygons.some(p => inRing(place.lat, place.lon, p)))
        .sort((a, b) => b.level - a.level || (a.onset ?? 0) - (b.onset ?? 0))
        .filter(w => {
            const key = `${w.hazard}|${w.level}|${w.onset}`;
            if (seen.has(key))
                return false;
            seen.add(key);
            return true;
        });
}

// Orange and red warnings bring a notification, each once.
export function toNotify(alerts, notified) {
    const done = new Set(notified ?? []);
    return (alerts ?? []).filter(a => a.level >= LEVELS.orange && !done.has(a.id));
}

// The original words of a warning from its CAP message, unchanged, in
// the person's language when the service wrote one, else English, else
// the first: {language, headline, description, sender, web}.
export function parseCap(text, languages) {
    const xml = String(text ?? '');
    if (xml.length > MAX_FEED_BYTES)
        return null;
    const infos = [...xml.matchAll(/<info>([\s\S]*?)<\/info>/g)].slice(0, 50).map(m => ({
        language: tag(m[1], 'language') ?? '',
        headline: tag(m[1], 'headline') ?? tag(m[1], 'event') ?? '',
        description: tag(m[1], 'description') ?? '',
        sender: tag(m[1], 'senderName') ?? '',
        web: tag(m[1], 'web') ?? '',
    }));
    if (!infos.length)
        return null;
    const want = (languages ?? []).map(l => String(l).slice(0, 2).toLowerCase());
    const by = code => infos.find(i => i.language.slice(0, 2).toLowerCase() === code);
    return want.map(by).find(Boolean) ?? by('en') ?? infos[0];
}

// The newest boundaries file in MeteoAlarm's documents repository
// (gitlab.com/meteoalarm-pm-group/documents): the GitLab tree listing
// -> its raw URL, or null.
export const GEOCODES_TREE = 'https://gitlab.com/api/v4/projects/meteoalarm-pm-group%2Fdocuments/repository/tree?per_page=100';
export const GEOCODES_RAW = 'https://gitlab.com/meteoalarm-pm-group/documents/-/raw/master/';

export function latestGeocodes(treeJson) {
    let list;
    try {
        list = JSON.parse(treeJson);
    } catch {
        return null;
    }
    const names = (Array.isArray(list) ? list : [])
        .map(f => f?.name).filter(n => typeof n === 'string' && /^MeteoAlarm_Geocodes_\d{4}_\d{2}_\d{2}\.json$/.test(n))
        .sort();
    return names.length ? {name: names.at(-1), url: GEOCODES_RAW + names.at(-1)} : null;
}

// The boundaries file (GeoJSON, about 30 MB) and a place -> the codes of
// the zones it lies in. Only the codes are kept: the file is
// EUMETNET's and is never stored or passed on.
export function zonesAt(geojson, lat, lon) {
    if (!finite(lat) || !finite(lon))
        return [];
    const features = Array.isArray(geojson?.features) ? geojson.features : [];
    const codes = [];
    for (const f of features) {
        const code = f?.properties?.code;
        const g = f?.geometry;
        if (typeof code !== 'string' || !g)
            continue;
        const polygons = g.type === 'Polygon' ? [g.coordinates] : g.type === 'MultiPolygon' ? g.coordinates : [];
        const hit = (polygons ?? []).some(rings => {
            if (!Array.isArray(rings) || !rings.length)
                return false;
            // GeoJSON is [lon, lat].
            const flip = ring => (Array.isArray(ring) ? ring : []).map(p => [p?.[1], p?.[0]]);
            return inRing(lat, lon, flip(rings[0])) && !rings.slice(1).some(h => inRing(lat, lon, flip(h)));
        });
        if (hit && !codes.includes(code))
            codes.push(code);
    }
    return codes;
}

// The words, in the ten languages of Vilna (as lib/widgets.js). Our own
// short labels; the warning's original text is shown beside them.
const ALERT_KEYS = ['levels', 'hazards', 'until', 'from', 'issued', 'meteredPaused', 'meteredAsk', 'title', 'show',
    'metered', 'meteredHint', 'source', 'zone', 'zoneNone', 'open', 'translated'];

// The table's key for a GLib language name: "uk_UA.UTF-8" → "uk", and
// Valencian, "ca_ES.UTF-8@valencia" → "ca@valencia" (ADR 0009).
function languageCode(name) {
    const s = String(name);
    const code = s.split(/[_.@-]/)[0].toLowerCase();
    return code === 'ca' && /@valencia/i.test(s) ? 'ca@valencia' : code;
}

const ALERT_TEXT = {
    en: [['Yellow warning', 'Orange warning', 'Red warning'],
        ['Wind', 'Snow and ice', 'Thunderstorms', 'Fog', 'Heat', 'Cold', 'Coastal event', 'Forest fire', 'Avalanches',
            'Rain', 'Flooding', 'Rain and flooding', 'Marine hazard', 'Drought', 'Weather'],
        'until %s', 'from %s', '%s · issued %s', 'Metered connection: warnings paused', 'Download warnings',
        'Weather warnings', 'Show weather warnings', 'Download on metered connections',
        'The country’s warnings, up to 0.5 MB every 10 minutes; zone boundaries, about 30 MB once per place.',
        'Warnings: EUMETNET – MeteoAlarm', 'Warning zone: %s', 'Warning zone not found yet', 'Open meteoalarm.org', 'translated by Gloxi'],
    uk: [['Жовте попередження', 'Помаранчеве попередження', 'Червоне попередження'],
        ['Вітер', 'Сніг і ожеледь', 'Грози', 'Туман', 'Спека', 'Холод', 'Явища на узбережжі', 'Лісові пожежі', 'Лавини',
            'Дощ', 'Повінь', 'Дощ і повінь', 'Небезпека на морі', 'Посуха', 'Погода'],
        'до %s', 'з %s', '%s · видано %s', 'Лімітне з’єднання: попередження на паузі', 'Завантажувати попередження',
        'Попередження про погоду', 'Показувати попередження про погоду', 'Завантажувати на лімітному з’єднанні',
        'Попередження країни — до 0,5 МБ раз на 10 хвилин; межі зон — близько 30 МБ один раз для місця.',
        'Попередження: EUMETNET – MeteoAlarm', 'Зона попереджень: %s', 'Зону попереджень ще не визначено',
        'Відкрити meteoalarm.org', 'переклад Gloxi'],
    es: [['Aviso amarillo', 'Aviso naranja', 'Aviso rojo'],
        ['Viento', 'Nieve y hielo', 'Tormentas', 'Niebla', 'Calor', 'Frío', 'Fenómenos costeros', 'Incendios forestales',
            'Aludes', 'Lluvia', 'Inundaciones', 'Lluvia e inundaciones', 'Peligro en el mar', 'Sequía', 'Tiempo'],
        'hasta las %s', 'desde las %s', '%s · emitido %s', 'Conexión medida: avisos en pausa', 'Descargar avisos',
        'Avisos meteorológicos', 'Mostrar avisos meteorológicos', 'Descargar con conexión medida',
        'Avisos del país: hasta 0,5 MB cada 10 minutos; límites de las zonas: unos 30 MB una vez por lugar.',
        'Avisos: EUMETNET – MeteoAlarm', 'Zona de avisos: %s', 'Aún no se ha encontrado la zona de avisos',
        'Abrir meteoalarm.org', 'traducido por Gloxi'],
    pl: [['Żółte ostrzeżenie', 'Pomarańczowe ostrzeżenie', 'Czerwone ostrzeżenie'],
        ['Wiatr', 'Śnieg i lód', 'Burze', 'Mgła', 'Upał', 'Mróz', 'Zjawiska na wybrzeżu', 'Pożary lasów', 'Lawiny',
            'Deszcz', 'Powódź', 'Deszcz i powódź', 'Zagrożenie na morzu', 'Susza', 'Pogoda'],
        'do %s', 'od %s', '%s · wydano %s', 'Połączenie taryfowe: ostrzeżenia wstrzymane', 'Pobieraj ostrzeżenia',
        'Ostrzeżenia pogodowe', 'Pokazuj ostrzeżenia pogodowe', 'Pobieraj przy połączeniu taryfowym',
        'Ostrzeżenia kraju: do 0,5 MB co 10 minut; granice stref: około 30 MB raz na miejsce.',
        'Ostrzeżenia: EUMETNET – MeteoAlarm', 'Strefa ostrzeżeń: %s', 'Nie znaleziono jeszcze strefy ostrzeżeń',
        'Otwórz meteoalarm.org', 'przetłumaczył Gloxi'],
    de: [['Gelbe Warnung', 'Orange Warnung', 'Rote Warnung'],
        ['Wind', 'Schnee und Eis', 'Gewitter', 'Nebel', 'Hitze', 'Kälte', 'Küstenereignis', 'Waldbrand', 'Lawinen',
            'Regen', 'Hochwasser', 'Regen und Hochwasser', 'Gefahr auf See', 'Dürre', 'Wetter'],
        'bis %s', 'ab %s', '%s · ausgegeben %s', 'Getaktete Verbindung: Warnungen pausiert', 'Warnungen laden',
        'Wetterwarnungen', 'Wetterwarnungen anzeigen', 'Bei getakteter Verbindung laden',
        'Warnungen des Landes: bis 0,5 MB alle 10 Minuten; Zonengrenzen: etwa 30 MB einmal pro Ort.',
        'Warnungen: EUMETNET – MeteoAlarm', 'Warnzone: %s', 'Warnzone noch nicht gefunden', 'meteoalarm.org öffnen', 'übersetzt von Gloxi'],
    fr: [['Vigilance jaune', 'Vigilance orange', 'Vigilance rouge'],
        ['Vent', 'Neige et verglas', 'Orages', 'Brouillard', 'Canicule', 'Grand froid', 'Phénomène côtier',
            'Feux de forêt', 'Avalanches', 'Pluie', 'Inondations', 'Pluie et inondations', 'Danger en mer', 'Sécheresse',
            'Météo'],
        'jusqu’à %s', 'à partir de %s', '%s · émis %s', 'Connexion limitée : vigilances en pause',
        'Télécharger les vigilances', 'Vigilances météo', 'Afficher les vigilances météo',
        'Télécharger sur une connexion limitée',
        'Vigilances du pays : jusqu’à 0,5 Mo toutes les 10 minutes ; limites des zones : environ 30 Mo une fois par lieu.',
        'Vigilances : EUMETNET – MeteoAlarm', 'Zone de vigilance : %s', 'Zone de vigilance pas encore trouvée',
        'Ouvrir meteoalarm.org', 'traduit par Gloxi'],
    it: [['Allerta gialla', 'Allerta arancione', 'Allerta rossa'],
        ['Vento', 'Neve e ghiaccio', 'Temporali', 'Nebbia', 'Caldo', 'Freddo', 'Fenomeni costieri', 'Incendi boschivi',
            'Valanghe', 'Pioggia', 'Alluvioni', 'Pioggia e alluvioni', 'Pericolo in mare', 'Siccità', 'Meteo'],
        'fino alle %s', 'dalle %s', '%s · emesso %s', 'Connessione a consumo: allerte in pausa', 'Scarica le allerte',
        'Allerte meteo', 'Mostra le allerte meteo', 'Scarica con connessione a consumo',
        'Allerte del paese: fino a 0,5 MB ogni 10 minuti; confini delle zone: circa 30 MB una volta per luogo.',
        'Allerte: EUMETNET – MeteoAlarm', 'Zona di allerta: %s', 'Zona di allerta non ancora trovata',
        'Apri meteoalarm.org', 'tradotto da Gloxi'],
    ro: [['Cod galben', 'Cod portocaliu', 'Cod roșu'],
        ['Vânt', 'Zăpadă și polei', 'Furtuni', 'Ceață', 'Caniculă', 'Ger', 'Fenomene pe coastă', 'Incendii de pădure',
            'Avalanșe', 'Ploaie', 'Inundații', 'Ploaie și inundații', 'Pericol pe mare', 'Secetă', 'Vreme'],
        'până la %s', 'de la %s', '%s · emis %s', 'Conexiune contorizată: avertizări în pauză',
        'Descarcă avertizările', 'Avertizări meteo', 'Arată avertizările meteo', 'Descarcă pe conexiune contorizată',
        'Avertizările țării: până la 0,5 MB la 10 minute; limitele zonelor: circa 30 MB o dată pentru fiecare loc.',
        'Avertizări: EUMETNET – MeteoAlarm', 'Zona de avertizare: %s', 'Zona de avertizare nu a fost găsită încă',
        'Deschide meteoalarm.org', 'tradus de Gloxi'],
    cs: [['Žlutá výstraha', 'Oranžová výstraha', 'Červená výstraha'],
        ['Vítr', 'Sníh a led', 'Bouřky', 'Mlha', 'Vedro', 'Mráz', 'Jevy na pobřeží', 'Lesní požáry', 'Laviny',
            'Déšť', 'Povodně', 'Déšť a povodně', 'Nebezpečí na moři', 'Sucho', 'Počasí'],
        'do %s', 'od %s', '%s · vydáno %s', 'Měřené připojení: výstrahy pozastaveny', 'Stahovat výstrahy',
        'Výstrahy před počasím', 'Zobrazovat výstrahy před počasím', 'Stahovat při měřeném připojení',
        'Výstrahy země: až 0,5 MB každých 10 minut; hranice oblastí: asi 30 MB jednou pro místo.',
        'Výstrahy: EUMETNET – MeteoAlarm', 'Výstražná oblast: %s', 'Výstražná oblast zatím nenalezena',
        'Otevřít meteoalarm.org', 'přeložil Gloxi'],
    lt: [['Geltonas įspėjimas', 'Oranžinis įspėjimas', 'Raudonas įspėjimas'],
        ['Vėjas', 'Sniegas ir plikledis', 'Perkūnija', 'Rūkas', 'Karštis', 'Šaltis', 'Reiškiniai pakrantėje',
            'Miškų gaisrai', 'Lavinos', 'Lietus', 'Potvyniai', 'Lietus ir potvyniai', 'Pavojus jūroje', 'Sausra', 'Orai'],
        'iki %s', 'nuo %s', '%s · paskelbta %s', 'Ribotas ryšys: įspėjimai sustabdyti', 'Atsisiųsti įspėjimus',
        'Įspėjimai apie orus', 'Rodyti įspėjimus apie orus', 'Atsisiųsti ribotu ryšiu',
        'Šalies įspėjimai – iki 0,5 MB kas 10 minučių; zonų ribos – apie 30 MB vieną kartą vietai.',
        'Įspėjimai: EUMETNET – MeteoAlarm', 'Įspėjimų zona: %s', 'Įspėjimų zona dar nerasta', 'Atidaryti meteoalarm.org', 'išvertė Gloxi'],
    ca: [['Avís groc', 'Avís taronja', 'Avís vermell'],
        ['Vent', 'Neu i gel', 'Tempestes', 'Boira', 'Calor', 'Fred', 'Fenòmens costaners', 'Incendis forestals', 'Allaus', 'Pluja', 'Inundacions', 'Pluja i inundacions', 'Perill al mar', 'Sequera', 'Temps'],
        'fins a les %s', 'des de les %s', '%s · emès %s', 'Connexió amb límit de dades: avisos en pausa',
        'Baixa els avisos', 'Avisos meteorològics', 'Mostra els avisos meteorològics',
        'Baixa amb connexions amb límit de dades',
        'Avisos del país: fins a 0,5 MB cada 10 minuts; límits de les zones: uns 30 MB una vegada per lloc.',
        'Avisos: EUMETNET – MeteoAlarm', 'Zona d’avisos: %s', 'Encara no s’ha trobat la zona d’avisos',
        'Obre meteoalarm.org', 'traduït per Gloxi'],
    'ca@valencia': [['Avís groc', 'Avís taronja', 'Avís vermell'],
        ['Vent', 'Neu i gel', 'Tempestes', 'Boira', 'Calor', 'Fred', 'Fenòmens costaners', 'Incendis forestals', 'Allaus', 'Pluja', 'Inundacions', 'Pluja i inundacions', 'Perill al mar', 'Sequera', 'Temps'],
        'fins a les %s', 'des de les %s', '%s · emès %s', 'Connexió amb límit de dades: avisos en pausa',
        'Baixa els avisos', 'Avisos meteorològics', 'Mostra els avisos meteorològics',
        'Baixa amb connexions amb límit de dades',
        'Avisos del país: fins a 0,5 MB cada 10 minuts; límits de les zones: uns 30 MB una vegada per lloc.',
        'Avisos: EUMETNET – MeteoAlarm', 'Zona d’avisos: %s', 'Encara no s’ha trobat la zona d’avisos',
        'Obre meteoalarm.org', 'traduït per Gloxi'],
    gl: [['Aviso amarelo', 'Aviso laranxa', 'Aviso vermello'],
        ['Vento', 'Neve e xeo', 'Treboadas', 'Néboa', 'Calor', 'Frío', 'Fenómenos costeiros', 'Incendios forestais', 'Alancadas', 'Choiva', 'Inundacións', 'Choiva e inundacións', 'Perigo no mar', 'Seca', 'Tempo'],
        'ata as %s', 'desde as %s', '%s · emitido %s', 'Conexión limitada: avisos en pausa', 'Descargar avisos',
        'Avisos meteorolóxicos', 'Mostrar avisos meteorolóxicos', 'Descargar con conexión limitada',
        'Avisos do país: ata 0,5 MB cada 10 minutos; límites das zonas: uns 30 MB unha vez por lugar.',
        'Avisos: EUMETNET – MeteoAlarm', 'Zona de avisos: %s', 'Aínda non se atopou a zona de avisos',
        'Abrir meteoalarm.org', 'traducido por Gloxi'],
    eu: [['Abisu horia', 'Abisu laranja', 'Abisu gorria'],
        ['Haizea', 'Elurra eta izotza', 'Ekaitzak', 'Lainoa', 'Beroa', 'Hotza', 'Kostaldeko fenomenoak', 'Baso-suteak', 'Elur-jausiak', 'Euria', 'Uholdeak', 'Euria eta uholdeak', 'Itsasoko arriskua', 'Lehortea', 'Eguraldia'],
        '%s arte', '%s(e)tik', '%s · igorria: %s', 'Konexio mugatua: abisuak pausatuta', 'Deskargatu abisuak',
        'Eguraldi-abisuak', 'Erakutsi eguraldi-abisuak', 'Deskargatu konexio mugatuetan',
        'Herrialdeko abisuak: 0,5 MB arte 10 minuturo; eremuen mugak: 30 MB inguru behin leku bakoitzeko.',
        'Abisuak: EUMETNET – MeteoAlarm', 'Abisu-eremua: %s', 'Abisu-eremua ez da oraindik aurkitu',
        'Ireki meteoalarm.org', 'Gloxik itzulia'],
    pt: [['Aviso amarelo', 'Aviso laranja', 'Aviso vermelho'],
        ['Vento', 'Neve e gelo', 'Trovoadas', 'Nevoeiro', 'Calor', 'Frio', 'Fenómenos costeiros', 'Incêndios florestais', 'Avalanches', 'Chuva', 'Inundações', 'Chuva e inundações', 'Perigo no mar', 'Seca', 'Tempo'],
        'até às %s', 'a partir das %s', '%s · emitido %s', 'Ligação limitada: avisos em pausa', 'Transferir avisos',
        'Avisos meteorológicos', 'Mostrar avisos meteorológicos', 'Transferir em ligações limitadas',
        'Avisos do país: até 0,5 MB a cada 10 minutos; limites das zonas: cerca de 30 MB uma vez por local.',
        'Avisos: EUMETNET – MeteoAlarm', 'Zona de avisos: %s', 'Zona de avisos ainda não encontrada',
        'Abrir meteoalarm.org', 'traduzido pelo Gloxi'],
};

export const ALERT_LANGUAGES = Object.keys(ALERT_TEXT);

// languageNames: GLib.get_language_names(), most wanted first.
export function alertText(languageNames) {
    let code = 'en';
    for (const name of languageNames ?? []) {
        const c = languageCode(name);
        if (ALERT_TEXT[c]) {
            code = c;
            break;
        }
    }
    return Object.fromEntries(ALERT_KEYS.map((key, i) => [key, ALERT_TEXT[code][i]]));
}

export function checkAlertTables() {
    return ALERT_LANGUAGES.filter(code => ALERT_TEXT[code].length !== ALERT_KEYS.length ||
        ALERT_TEXT[code][0].length !== 3 || ALERT_TEXT[code][1].length !== HAZARDS.length + 1);
}

// The language Gloxi should put a warning's words into, or null when they
// already are in the person's language (they are then shown as they are and
// Gloxi is not asked; Denys, 18.11.2026). The person's language is the one
// our labels use (alertText): the first of theirs that Vilna speaks.
export function translationTarget(original, languageNames) {
    if (!original?.headline)
        return null;
    let target = 'en';
    for (const name of languageNames ?? []) {
        const c = languageCode(name);
        if (ALERT_TEXT[c]) {
            target = c;
            break;
        }
    }
    const language = String(original.language ?? '').slice(0, 2).toLowerCase();
    // A warning in Catalan is already in the language of a Valencian person.
    return language === target.split('@')[0] ? null : target;
}

// Our short label for a warning: "Orange warning: Wind".
export function alertLabel(alert, t) {
    const level = t.levels[Math.min(2, Math.max(0, (alert?.level ?? 2) - 2))];
    const i = HAZARDS.indexOf(alert?.hazard);
    return `${level}: ${t.hazards[i >= 0 ? i : HAZARDS.length]}`;
}

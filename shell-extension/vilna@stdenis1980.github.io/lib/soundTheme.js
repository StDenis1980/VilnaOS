// SPDX-License-Identifier: GPL-3.0-or-later
//
// The person's sound choices as a freedesktop sound theme (pure logic).
//
// GNOME plays event sounds from one theme (org.gnome.desktop.sound
// theme-name). Vilna points it at a small theme of the person's own,
// ~/.local/share/sounds/vilna-user: it inherits the Vilna theme they chose
// and holds an empty <event>.disabled file for every sound they turned off,
// which the Sound Theme spec (and libcanberra) read as "play nothing".
// Turning all sounds off is GNOME's own event-sounds switch.

export const USER_THEME = 'vilna-user';

// The themes sounds/make_sounds.py makes (package vilna-sound-themes), plus GNOME's default.
export const SOUND_THEMES = ['wood', 'bell', 'water', 'air', 'birds', 'glass', 'star', 'freedesktop'];

// The events a Vilna theme has (MOTIFS in make_sounds.py), so the ones a
// person can turn off one by one.
export const SOUND_EVENTS = [
    'message-new-instant', 'bell', 'dialog-warning', 'dialog-error', 'complete',
    'screen-capture', 'audio-volume-change', 'device-added', 'device-removed',
    'power-plug', 'power-unplug', 'battery-low', 'trash-empty', 'desktop-login',
    'x-vilna-gloxi-listen', 'x-vilna-gloxi-done', 'x-vilna-gloxi-error',
    'x-vilna-herkos-consent',
];

// The theme the user theme inherits: a known name, else Vilna's default.
export function baseTheme(theme) {
    if (!SOUND_THEMES.includes(theme))
        theme = SOUND_THEMES[0];
    return theme === 'freedesktop' ? theme : `vilna-${theme}`;
}

export function indexTheme(theme) {
    return [
        '[Sound Theme]',
        'Name=Vilna (my choice)',
        'Name[uk]=Vilna (мій вибір)',
        `Inherits=${baseTheme(theme)}`,
        'Directories=stereo',
        '',
        '[stereo]',
        'OutputProfile=stereo',
        '',
    ].join('\n');
}

// The .disabled files the theme should hold: known events only, so a
// setting written by hand cannot name a path.
export function disabledFiles(disabled) {
    const off = new Set((disabled ?? []).filter(e => SOUND_EVENTS.includes(e)));
    return SOUND_EVENTS.filter(e => off.has(e)).map(e => `${e}.disabled`);
}

// What to do in the theme folder: write index.theme if it differs, create
// the missing .disabled files and remove the stale ones.
export function plan(theme, disabled, existingIndex, existingFiles) {
    const index = indexTheme(theme);
    const want = new Set(disabledFiles(disabled));
    const have = new Set((existingFiles ?? []).filter(f => f.endsWith('.disabled')));
    return {
        index: existingIndex === index ? null : index,
        create: [...want].filter(f => !have.has(f)),
        remove: [...have].filter(f => !want.has(f)),
    };
}

// The words of the Sounds page, in the ten languages of Vilna (the
// extension has no gettext catalogue of its own).
const UI_KEYS = ['page', 'groupTheme', 'theme', 'all', 'allHint', 'groupEvents', 'eventsHint', 'play'];

const SOUND_TEXT = {
    en: {
        ui: ['Sounds', 'Sound theme', 'Theme', 'Event sounds', 'Turn all sounds on or off at once', 'Sounds one by one',
            'Turn off the sounds you do not want; the others keep playing.', 'Play'],
        themes: ['Wood', 'Bell', 'Water', 'Air', 'Birds', 'Glass', 'Star', 'GNOME standard'],
        events: ['Notification', 'Alert sound', 'Warning', 'Error', 'Done', 'Screenshot', 'Volume', 'Device connected',
            'Device removed', 'Charger connected', 'Charger removed', 'Battery low', 'Trash emptied', 'Login',
            'Gloxi is listening', 'Gloxi has finished', 'Gloxi: a problem', 'Permission request'],
    },
    uk: {
        ui: ['Звуки', 'Звукова тема', 'Тема', 'Звуки подій', 'Увімкнути чи вимкнути всі звуки разом', 'Окремі звуки',
            'Вимкніть звуки, які вам не потрібні; решта гратиме далі.', 'Прослухати'],
        themes: ['Дерево', 'Дзвіночок', 'Вода', 'Повітря', 'Птахи', 'Скло', 'Зірка', 'Стандартні GNOME'],
        events: ['Сповіщення', 'Звуковий сигнал', 'Попередження', 'Помилка', 'Готово', 'Знімок екрана', 'Гучність',
            'Пристрій під’єднано', 'Пристрій від’єднано', 'Заряджання під’єднано', 'Заряджання від’єднано',
            'Низький заряд', 'Кошик очищено', 'Вхід у систему', 'Gloxi слухає', 'Gloxi завершив', 'Gloxi: проблема',
            'Запит дозволу'],
    },
    es: {
        ui: ['Sonidos', 'Tema de sonido', 'Tema', 'Sonidos de eventos', 'Activar o desactivar todos los sonidos a la vez',
            'Sonidos uno a uno', 'Desactive los sonidos que no quiera; los demás seguirán sonando.', 'Escuchar'],
        themes: ['Madera', 'Campanilla', 'Agua', 'Aire', 'Pájaros', 'Cristal', 'Estrella', 'Estándar de GNOME'],
        events: ['Notificación', 'Aviso sonoro', 'Advertencia', 'Error', 'Hecho', 'Captura de pantalla', 'Volumen',
            'Dispositivo conectado', 'Dispositivo desconectado', 'Cargador conectado', 'Cargador desconectado',
            'Batería baja', 'Papelera vaciada', 'Inicio de sesión', 'Gloxi escucha', 'Gloxi ha terminado',
            'Gloxi: un problema', 'Solicitud de permiso'],
    },
    pl: {
        ui: ['Dźwięki', 'Motyw dźwiękowy', 'Motyw', 'Dźwięki zdarzeń', 'Włącz lub wyłącz wszystkie dźwięki naraz',
            'Dźwięki pojedynczo', 'Wyłącz dźwięki, których nie chcesz; pozostałe nadal będą grać.', 'Odtwórz'],
        themes: ['Drewno', 'Dzwonek', 'Woda', 'Powietrze', 'Ptaki', 'Szkło', 'Gwiazda', 'Standardowe GNOME'],
        events: ['Powiadomienie', 'Sygnał dźwiękowy', 'Ostrzeżenie', 'Błąd', 'Gotowe', 'Zrzut ekranu', 'Głośność',
            'Urządzenie podłączone', 'Urządzenie odłączone', 'Ładowarka podłączona', 'Ładowarka odłączona',
            'Niski poziom baterii', 'Kosz opróżniony', 'Logowanie', 'Gloxi słucha', 'Gloxi skończył', 'Gloxi: problem',
            'Prośba o zgodę'],
    },
    de: {
        ui: ['Klänge', 'Klangthema', 'Thema', 'Ereignisklänge', 'Alle Klänge auf einmal ein- oder ausschalten',
            'Klänge einzeln', 'Schalten Sie Klänge aus, die Sie nicht möchten; die anderen spielen weiter.', 'Abspielen'],
        themes: ['Holz', 'Glöckchen', 'Wasser', 'Luft', 'Vögel', 'Glas', 'Stern', 'GNOME-Standard'],
        events: ['Benachrichtigung', 'Signalton', 'Warnung', 'Fehler', 'Fertig', 'Bildschirmfoto', 'Lautstärke',
            'Gerät verbunden', 'Gerät getrennt', 'Ladegerät angeschlossen', 'Ladegerät getrennt', 'Akku schwach',
            'Papierkorb geleert', 'Anmeldung', 'Gloxi hört zu', 'Gloxi ist fertig', 'Gloxi: ein Problem',
            'Berechtigungsanfrage'],
    },
    fr: {
        ui: ['Sons', 'Thème sonore', 'Thème', 'Sons des événements', 'Activer ou désactiver tous les sons à la fois',
            'Sons un par un', 'Désactivez les sons dont vous ne voulez pas ; les autres continuent de jouer.', 'Écouter'],
        themes: ['Bois', 'Clochette', 'Eau', 'Air', 'Oiseaux', 'Verre', 'Étoile', 'Standard GNOME'],
        events: ['Notification', 'Signal sonore', 'Avertissement', 'Erreur', 'Terminé', 'Capture d’écran', 'Volume',
            'Appareil connecté', 'Appareil déconnecté', 'Chargeur branché', 'Chargeur débranché', 'Batterie faible',
            'Corbeille vidée', 'Connexion', 'Gloxi écoute', 'Gloxi a terminé', 'Gloxi : un problème',
            'Demande d’autorisation'],
    },
    it: {
        ui: ['Suoni', 'Tema sonoro', 'Tema', 'Suoni degli eventi', 'Attiva o disattiva tutti i suoni insieme',
            'Suoni uno per uno', 'Disattiva i suoni che non vuoi; gli altri continuano a suonare.', 'Ascolta'],
        themes: ['Legno', 'Campanella', 'Acqua', 'Aria', 'Uccelli', 'Vetro', 'Stella', 'Standard GNOME'],
        events: ['Notifica', 'Segnale acustico', 'Avviso', 'Errore', 'Fatto', 'Screenshot', 'Volume',
            'Dispositivo collegato', 'Dispositivo scollegato', 'Caricatore collegato', 'Caricatore scollegato',
            'Batteria scarica', 'Cestino svuotato', 'Accesso', 'Gloxi ascolta', 'Gloxi ha finito', 'Gloxi: un problema',
            'Richiesta di autorizzazione'],
    },
    ro: {
        ui: ['Sunete', 'Temă de sunete', 'Temă', 'Sunete pentru evenimente', 'Pornește sau oprește toate sunetele deodată',
            'Sunete pe rând', 'Opriți sunetele pe care nu le doriți; celelalte continuă să sune.', 'Ascultă'],
        themes: ['Lemn', 'Clopoțel', 'Apă', 'Aer', 'Păsări', 'Sticlă', 'Stea', 'Standard GNOME'],
        events: ['Notificare', 'Semnal sonor', 'Avertisment', 'Eroare', 'Gata', 'Captură de ecran', 'Volum',
            'Dispozitiv conectat', 'Dispozitiv deconectat', 'Încărcător conectat', 'Încărcător deconectat',
            'Baterie descărcată', 'Coș golit', 'Autentificare', 'Gloxi ascultă', 'Gloxi a terminat',
            'Gloxi: o problemă', 'Cerere de permisiune'],
    },
    cs: {
        ui: ['Zvuky', 'Zvukový motiv', 'Motiv', 'Zvuky událostí', 'Zapnout nebo vypnout všechny zvuky najednou',
            'Jednotlivé zvuky', 'Vypněte zvuky, které nechcete; ostatní budou hrát dál.', 'Přehrát'],
        themes: ['Dřevo', 'Zvoneček', 'Voda', 'Vzduch', 'Ptáci', 'Sklo', 'Hvězda', 'Standardní GNOME'],
        events: ['Oznámení', 'Zvukový signál', 'Varování', 'Chyba', 'Hotovo', 'Snímek obrazovky', 'Hlasitost',
            'Zařízení připojeno', 'Zařízení odpojeno', 'Nabíječka připojena', 'Nabíječka odpojena', 'Slabá baterie',
            'Koš vysypán', 'Přihlášení', 'Gloxi poslouchá', 'Gloxi dokončil', 'Gloxi: problém', 'Žádost o povolení'],
    },
    lt: {
        ui: ['Garsai', 'Garsų tema', 'Tema', 'Įvykių garsai', 'Įjungti arba išjungti visus garsus iš karto',
            'Garsai po vieną', 'Išjunkite nenorimus garsus; kiti skambės toliau.', 'Klausyti'],
        themes: ['Medis', 'Varpelis', 'Vanduo', 'Oras', 'Paukščiai', 'Stiklas', 'Žvaigždė', 'GNOME standartiniai'],
        events: ['Pranešimas', 'Garso signalas', 'Įspėjimas', 'Klaida', 'Atlikta', 'Ekrano kopija', 'Garsumas',
            'Įrenginys prijungtas', 'Įrenginys atjungtas', 'Įkroviklis prijungtas', 'Įkroviklis atjungtas',
            'Senka baterija', 'Šiukšlinė ištuštinta', 'Prisijungimas', 'Gloxi klausosi', 'Gloxi baigė',
            'Gloxi: problema', 'Leidimo prašymas'],
    },
};

export const SOUND_LANGUAGES = Object.keys(SOUND_TEXT);

// languageNames: GLib.get_language_names(), most wanted first.
export function soundText(languageNames) {
    let code = 'en';
    for (const name of languageNames ?? []) {
        const c = String(name).split(/[_.@-]/)[0].toLowerCase();
        if (SOUND_TEXT[c]) {
            code = c;
            break;
        }
    }
    const t = SOUND_TEXT[code];
    return {
        ...Object.fromEntries(UI_KEYS.map((key, i) => [key, t.ui[i]])),
        themes: Object.fromEntries(SOUND_THEMES.map((theme, i) => [theme, t.themes[i]])),
        events: Object.fromEntries(SOUND_EVENTS.map((event, i) => [event, t.events[i]])),
    };
}

// Languages whose tables are not the right length (a test keeps this empty).
export function checkSoundTables() {
    return SOUND_LANGUAGES.filter(code => {
        const t = SOUND_TEXT[code];
        return t.ui.length !== UI_KEYS.length || t.themes.length !== SOUND_THEMES.length ||
            t.events.length !== SOUND_EVENTS.length;
    });
}

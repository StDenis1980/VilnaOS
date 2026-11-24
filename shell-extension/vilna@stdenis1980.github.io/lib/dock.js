// SPDX-License-Identifier: GPL-3.0-or-later
//
// (і) The dock's settings and motion (pure logic, no GNOME imports, so
// that node can test it). The dock is Dash to Dock; its own keys
// (org.gnome.shell.extensions.dash-to-dock) hold the place, the icon size
// and hiding. Vilna adds the transparency of the glass and the springy
// motion of the icons (dockLook.js, dockMotion.js, prefs.js).

// Icon size in px. Below 32 an icon is hard to hit and to recognise;
// above 80 the dock with its padding takes more than a tenth of a
// 1366×768 laptop screen. 48 is GNOME's own dash size.
export const ICON_SIZE_MIN = 32;
export const ICON_SIZE_MAX = 80;
export const ICON_SIZE_STEP = 4;
export const ICON_SIZE_DEFAULT = 48;

export function clampIconSize(size) {
    const n = Math.round(Number(size));
    if (!Number.isFinite(n))
        return ICON_SIZE_DEFAULT;
    return Math.min(ICON_SIZE_MAX, Math.max(ICON_SIZE_MIN, n));
}

// Places: the top edge is the top bar's and the right edge is Gloxi's side
// panel (dock.js, a strut). Dash to Dock centres a bottom dock in the work
// area, so it stays left of an open panel; a dock on the right would sit on
// top of the panel, so it is not offered.
export const POSITIONS = ['BOTTOM', 'LEFT'];

// How the dock hides, as Dash to Dock's three keys.
export const HIDE_MODES = ['never', 'windows', 'always'];

export function hideKeys(mode) {
    switch (mode) {
    case 'never':
        return {'dock-fixed': true, 'intellihide': true, 'autohide': true};
    case 'always':
        return {'dock-fixed': false, 'intellihide': false, 'autohide': true};
    default:
        return {'dock-fixed': false, 'intellihide': true, 'autohide': true};
    }
}

// The mode the keys say, for the preferences to show; a mix set by hand
// that none of the modes matches reads as the closest one.
export function hideMode({dockFixed, intellihide}) {
    if (dockFixed)
        return 'never';
    return intellihide ? 'windows' : 'always';
}

// Transparency of the glass: 0 % solid, 100 % only the icons. 8 % matches
// the terminal's 92 % opacity (Ptyxis profile Vilna).
export const TRANSPARENCY_DEFAULT = 8;

export function glassAlpha(transparency) {
    const t = Number(transparency);
    if (!Number.isFinite(t))
        return 1 - TRANSPARENCY_DEFAULT / 100;
    return Math.round((1 - Math.min(100, Math.max(0, t)) / 100) * 1000) / 1000;
}

// Colours of docs/design/style/vilna.css: cream glass in the light style,
// night blue in the dark one; the border and the hover tint fade with the
// glass so that 100 % leaves nothing but the icons.
const SCHEMES = {
    'vilna-light': {glass: [255, 252, 246], line: [19, 33, 58, 0.1], separator: [19, 33, 58, 0.14], hover: [19, 33, 58, 0.08]},
    'vilna-dark': {glass: [14, 26, 46], line: [255, 255, 255, 0.1], separator: [255, 255, 255, 0.12], hover: [255, 255, 255, 0.1]},
};

const SIDES = ['bottom', 'left', 'right', 'top'];

const rgba = ([r, g, b], a) => `rgba(${r}, ${g}, ${b}, ${Math.round(a * 1000) / 1000})`;

// The stylesheet dockLook.js loads. Its selectors carry .vilna-dock (put on
// Main.uiGroup with the scheme class) and the side class, so that they
// outrank Dash to Dock's own (#dashtodockContainer.bottom #dash …) on
// every side the dock can take.
export function dockStylesheet(transparency) {
    const a = glassAlpha(transparency);
    const rules = [];
    for (const [scheme, c] of Object.entries(SCHEMES)) {
        for (const side of SIDES) {
            const dash = `.vilna-dock.${scheme} #dashtodockContainer.${side} #dash`;
            rules.push(`${dash} .dash-background {\n` +
                `  background-color: ${rgba(c.glass, a)};\n` +
                `  border: 1px solid ${rgba(c.line, c.line[3] * a)};\n` +
                '  border-radius: 24px;\n}');
            rules.push(`${dash} .dash-separator {\n  background-color: ${rgba(c.separator, c.separator[3] * Math.max(a, 0.5))};\n}`);
            rules.push([
                `${dash} .dash-item-container .overview-tile.focused .overview-icon`,
                `${dash} .dash-item-container .overview-tile:hover .overview-icon`,
                `${dash} .dash-item-container .show-apps:hover .overview-icon`,
            ].join(',\n') + ` {\n  background-color: ${rgba(c.hover, c.hover[3])};\n}`);
        }
    }
    return `/* Made by the Vilna extension (lib/dock.js); do not edit. */\n${rules.join('\n\n')}\n`;
}

// Icon motion: 'spring' or 'off'.
export const ICON_MOTIONS = ['spring', 'off'];

// Motion tokens (docs/design/motion/tokens.json): 150 ms micro, 280 ms
// panel, 450 ms dialog. Clutter's named modes stand in for the curves;
// EASE_OUT_BACK overshoots a little and settles, which reads as a spring.
export const HOVER_SCALE = 1.1;
export const PRESS_SCALE = 0.86;
export const JIGGLE_DEGREES = 2.5;
export const PULSE_SCALE = 0.92;
export const PULSE_MAX = 6;

// Where an icon grows from: the edge of the dock that touches the screen
// edge, so that it grows towards the desktop and stays inside the dock's
// padding (Dash to Dock clips what leaves it). side: 'BOTTOM' etc.
export function pivotFor(side) {
    switch (side) {
    case 'LEFT':
        return [0, 0.5];
    case 'RIGHT':
        return [1, 0.5];
    case 'TOP':
        return [0.5, 0];
    default:
        return [0.5, 1];
    }
}

// The motion of one icon for what just happened to it, or null for none.
// event: 'hover', 'leave', 'press', 'release', 'pulse' (the app is
// starting), 'jiggle' (another icon is dragged), 'still'.
// motion: the dock-icon-motion setting; animations: GNOME's
// enable-animations; reduced: the window-motion setting is 'reduced'.
export function iconMotion(event, {motion = 'spring', animations = true, reduced = false} = {}) {
    if (motion !== 'spring' || !animations || reduced)
        return event === 'still' || event === 'leave' || event === 'release'
            ? {scale: 1, rotation: 0, duration: 0, mode: 'EASE_OUT_QUAD'} : null;
    switch (event) {
    case 'hover':
        return {scale: HOVER_SCALE, rotation: 0, duration: 280, mode: 'EASE_OUT_BACK'};
    case 'leave':
        return {scale: 1, rotation: 0, duration: 280, mode: 'EASE_OUT_BACK'};
    case 'press':
        return {scale: PRESS_SCALE, rotation: 0, duration: 150, mode: 'EASE_OUT_QUAD'};
    case 'release':
        return {scale: HOVER_SCALE, rotation: 0, duration: 450, mode: 'EASE_OUT_ELASTIC'};
    case 'pulse':
        return {scale: PULSE_SCALE, rotation: 0, duration: 450, mode: 'EASE_IN_OUT_SINE', repeat: PULSE_MAX, autoReverse: true};
    case 'jiggle':
        return {scale: 1, rotation: JIGGLE_DEGREES, duration: 150, mode: 'EASE_IN_OUT_SINE', repeat: -1, autoReverse: true};
    case 'still':
        return {scale: 1, rotation: 0, duration: 150, mode: 'EASE_OUT_QUAD'};
    default:
        return null;
    }
}

// The words of the Dock page, in the ten languages of Vilna (the
// extension has no gettext catalogue of its own).
const UI_KEYS = [
    'page', 'groupLook', 'size', 'sizeHint', 'transparency', 'transparencyHint', 'groupPlace', 'position',
    'hide', 'motion', 'motionHint', 'missing',
];

const TEXT = {
    en: {
        ui: ['Dock', 'Look', 'Icon size', 'From 32 to 80 pixels', 'Transparency', '0 % solid glass, 100 % only the icons',
            'Place and hiding', 'Place', 'Hide the dock', 'Springy icons',
            'Icons grow under the pointer, spring back when clicked and sway while you move one',
            'The dock is not installed'],
        positions: ['Bottom', 'Left'],
        hides: ['Never', 'When a window covers it', 'Always, show at the edge'],
    },
    uk: {
        ui: ['Док', 'Вигляд', 'Розмір значків', 'Від 32 до 80 пікселів', 'Прозорість', '0 % — суцільне скло, 100 % — лише значки',
            'Місце і приховування', 'Місце', 'Ховати док', 'Пружні значки',
            'Значки ростуть під вказівником, пружинять після клацання і гойдаються, поки ви переставляєте інший',
            'Док не встановлено'],
        positions: ['Знизу', 'Ліворуч'],
        hides: ['Ніколи', 'Коли його закриває вікно', 'Завжди, показувати біля краю'],
    },
    es: {
        ui: ['Dock', 'Aspecto', 'Tamaño de los iconos', 'De 32 a 80 píxeles', 'Transparencia',
            '0 %: cristal opaco, 100 %: solo los iconos', 'Posición y ocultación', 'Posición', 'Ocultar el dock',
            'Iconos elásticos', 'Los iconos crecen bajo el puntero, rebotan al hacer clic y se balancean mientras mueve otro',
            'El dock no está instalado'],
        positions: ['Abajo', 'Izquierda'],
        hides: ['Nunca', 'Cuando una ventana lo tapa', 'Siempre, mostrar en el borde'],
    },
    pl: {
        ui: ['Dok', 'Wygląd', 'Rozmiar ikon', 'Od 32 do 80 pikseli', 'Przezroczystość',
            '0% — pełne szkło, 100% — tylko ikony', 'Położenie i ukrywanie', 'Położenie', 'Ukrywaj dok',
            'Sprężyste ikony', 'Ikony rosną pod wskaźnikiem, odskakują po kliknięciu i kołyszą się, gdy przesuwasz inną',
            'Dok nie jest zainstalowany'],
        positions: ['Na dole', 'Po lewej'],
        hides: ['Nigdy', 'Gdy zasłania go okno', 'Zawsze, pokazuj przy krawędzi'],
    },
    de: {
        ui: ['Dock', 'Aussehen', 'Symbolgröße', '32 bis 80 Pixel', 'Transparenz',
            '0 % volles Glas, 100 % nur die Symbole', 'Ort und Ausblenden', 'Ort', 'Dock ausblenden',
            'Federnde Symbole', 'Symbole wachsen unter dem Zeiger, federn beim Klicken zurück und wiegen sich, während Sie ein anderes verschieben',
            'Das Dock ist nicht installiert'],
        positions: ['Unten', 'Links'],
        hides: ['Nie', 'Wenn ein Fenster es verdeckt', 'Immer, am Rand zeigen'],
    },
    fr: {
        ui: ['Dock', 'Apparence', 'Taille des icônes', 'De 32 à 80 pixels', 'Transparence',
            '0 % verre plein, 100 % seulement les icônes', 'Position et masquage', 'Position', 'Masquer le dock',
            'Icônes élastiques', 'Les icônes grandissent sous le pointeur, rebondissent au clic et oscillent pendant que vous en déplacez une autre',
            'Le dock n’est pas installé'],
        positions: ['En bas', 'À gauche'],
        hides: ['Jamais', 'Quand une fenêtre le couvre', 'Toujours, afficher au bord'],
    },
    it: {
        ui: ['Dock', 'Aspetto', 'Dimensione delle icone', 'Da 32 a 80 pixel', 'Trasparenza',
            '0% vetro pieno, 100% solo le icone', 'Posizione e scomparsa', 'Posizione', 'Nascondi il dock',
            'Icone elastiche', 'Le icone crescono sotto il puntatore, rimbalzano al clic e oscillano mentre ne sposti un’altra',
            'Il dock non è installato'],
        positions: ['In basso', 'A sinistra'],
        hides: ['Mai', 'Quando una finestra lo copre', 'Sempre, mostra sul bordo'],
    },
    ro: {
        ui: ['Doc', 'Aspect', 'Mărimea pictogramelor', 'Între 32 și 80 de pixeli', 'Transparență',
            '0% sticlă plină, 100% doar pictogramele', 'Loc și ascundere', 'Loc', 'Ascunde docul',
            'Pictograme elastice', 'Pictogramele cresc sub indicator, revin elastic la clic și se leagănă cât mutați alta',
            'Docul nu este instalat'],
        positions: ['Jos', 'Stânga'],
        hides: ['Niciodată', 'Când o fereastră îl acoperă', 'Mereu, arată la margine'],
    },
    cs: {
        ui: ['Dok', 'Vzhled', 'Velikost ikon', '32 až 80 pixelů', 'Průhlednost',
            '0 % plné sklo, 100 % jen ikony', 'Umístění a skrývání', 'Umístění', 'Skrývat dok',
            'Pružné ikony', 'Ikony rostou pod ukazatelem, po kliknutí pruží a pohupují se, když přesouváte jinou',
            'Dok není nainstalován'],
        positions: ['Dole', 'Vlevo'],
        hides: ['Nikdy', 'Když ho zakryje okno', 'Vždy, ukázat u okraje'],
    },
    lt: {
        ui: ['Dokas', 'Išvaizda', 'Piktogramų dydis', 'Nuo 32 iki 80 taškų', 'Skaidrumas',
            '0 % – vientisas stiklas, 100 % – tik piktogramos', 'Vieta ir slėpimas', 'Vieta', 'Slėpti doką',
            'Spyruokliuojančios piktogramos', 'Piktogramos padidėja po žymekliu, spyruokliuoja spustelėjus ir siūbuoja, kol perkeliate kitą',
            'Dokas neįdiegtas'],
        positions: ['Apačioje', 'Kairėje'],
        hides: ['Niekada', 'Kai jį uždengia langas', 'Visada, rodyti prie krašto'],
    },
};

export const DOCK_LANGUAGES = Object.keys(TEXT);

// languageNames: GLib.get_language_names(), most wanted first.
export function dockText(languageNames) {
    let code = 'en';
    for (const name of languageNames ?? []) {
        const c = String(name).split(/[_.@-]/)[0].toLowerCase();
        if (TEXT[c]) {
            code = c;
            break;
        }
    }
    const t = TEXT[code];
    return {
        ...Object.fromEntries(UI_KEYS.map((key, i) => [key, t.ui[i]])),
        positions: Object.fromEntries(POSITIONS.map((p, i) => [p, t.positions[i]])),
        hides: Object.fromEntries(HIDE_MODES.map((m, i) => [m, t.hides[i]])),
    };
}

// Languages whose tables are not the right length (a test keeps this empty).
export function checkDockTables() {
    return DOCK_LANGUAGES.filter(code => {
        const t = TEXT[code];
        return t.ui.length !== UI_KEYS.length || t.positions.length !== POSITIONS.length ||
            t.hides.length !== HIDE_MODES.length;
    });
}

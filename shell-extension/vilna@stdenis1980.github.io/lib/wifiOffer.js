// SPDX-License-Identifier: GPL-3.0-or-later
//
// When to offer a VPN on a Wi-Fi network (ADR 0027, section 6).
//
// A network that asked for a login (a captive portal: hotel, cafe, airport)
// is a network other people share. Once the person is through the login page
// and the internet answers, the Shell offers a VPN, once per network, and
// never while a VPN tunnel is up. Pure logic, no GNOME imports, so that
// node can test it.

// Connection UUIDs remembered at most; the oldest are forgotten first.
export const MAX_REMEMBERED = 50;

export class WifiOffer {
    // offered: UUIDs of networks the offer was already shown for.
    constructor(offered = []) {
        this._offered = offered.filter(u => typeof u === 'string' && u).slice(-MAX_REMEMBERED);
        this._loginFor = null;
    }

    get offered() {
        return [...this._offered];
    }

    // connectivity: 'full', 'portal', 'limited', 'none' or 'unknown';
    // uuid: the primary connection; wifi: whether it is a Wi-Fi network;
    // vpnUp: whether a tunnel is up. Returns true when the offer should show.
    update({connectivity, uuid, wifi, vpnUp}) {
        if (!uuid || !wifi) {
            this._loginFor = null;
            return false;
        }
        if (this._loginFor !== uuid)
            this._loginFor = null;
        if (connectivity === 'portal' || connectivity === 'limited') {
            this._loginFor = uuid;
            return false;
        }
        if (connectivity !== 'full' || this._loginFor !== uuid)
            return false;
        this._loginFor = null;
        if (vpnUp || this._offered.includes(uuid))
            return false;
        this._offered = [...this._offered, uuid].slice(-MAX_REMEMBERED);
        return true;
    }
}

// The words, in the ten languages of Vilna (ADR 0009). The extension has no
// gettext catalogue of its own; these three lines are all it says.
// The table's key for a GLib language name: "uk_UA.UTF-8" → "uk", and
// Valencian, "ca_ES.UTF-8@valencia" → "ca@valencia" (ADR 0009).
function languageCode(name) {
    const s = String(name);
    const code = s.split(/[_.@-]/)[0].toLowerCase();
    return code === 'ca' && /@valencia/i.test(s) ? 'ca@valencia' : code;
}

const TEXT = {
    en: ['Shared Wi-Fi', 'DNS is already encrypted. A VPN hides the rest of your traffic from this network too.', 'Set up a VPN'],
    uk: ['Спільна Wi-Fi', 'DNS уже зашифровано. VPN сховає від цієї мережі й решту трафіку.', 'Налаштувати VPN'],
    es: ['Wi-Fi compartida', 'El DNS ya está cifrado. Una VPN oculta también el resto de tu tráfico a esta red.', 'Configurar una VPN'],
    pl: ['Wspólna sieć Wi-Fi', 'DNS jest już szyfrowany. VPN ukryje przed tą siecią także resztę ruchu.', 'Skonfiguruj VPN'],
    de: ['Geteiltes WLAN', 'DNS ist bereits verschlüsselt. Ein VPN verbirgt auch den restlichen Datenverkehr vor diesem Netz.', 'VPN einrichten'],
    fr: ['Wi-Fi partagé', 'Le DNS est déjà chiffré. Un VPN cache aussi le reste de votre trafic à ce réseau.', 'Configurer un VPN'],
    it: ['Wi-Fi condivisa', 'Il DNS è già cifrato. Una VPN nasconde a questa rete anche il resto del traffico.', 'Configura una VPN'],
    ro: ['Wi-Fi comun', 'DNS-ul este deja criptat. Un VPN ascunde de această rețea și restul traficului.', 'Configurează un VPN'],
    cs: ['Sdílená Wi-Fi', 'DNS je už šifrované. VPN skryje před touto sítí i zbytek provozu.', 'Nastavit VPN'],
    lt: ['Bendras „Wi-Fi“ tinklas', 'DNS jau užšifruotas. VPN nuo šio tinklo paslėps ir likusį srautą.', 'Nustatyti VPN'],
    ca: ['Wifi compartida', 'El DNS ja està xifrat. Una VPN també amaga a aquesta xarxa la resta del teu trànsit.',
        'Configura una VPN'],
    'ca@valencia': ['Wifi compartida',
        'El DNS ja està xifrat. Una VPN també amaga a esta xarxa la resta del teu trànsit.', 'Configura una VPN'],
    gl: ['Wifi compartida', 'O DNS xa está cifrado. Unha VPN tamén oculta a esta rede o resto do teu tráfico.',
        'Configurar unha VPN'],
    eu: ['Wifi partekatua',
        'DNSa zifratuta dago jada. VPN batek zure gainerako trafikoa ere ezkutatzen dio sare honi.',
        'Konfiguratu VPN bat'],
    pt: ['Wi-Fi partilhada', 'O DNS já está cifrado. Uma VPN também esconde desta rede o resto do seu tráfego.',
        'Configurar uma VPN'],
};

export const LANGUAGES = Object.keys(TEXT);

// languageNames: GLib.get_language_names(), most wanted first ('uk_UA.UTF-8', 'uk', 'C').
export function offerText(languageNames) {
    for (const name of languageNames ?? []) {
        const code = languageCode(name);
        if (TEXT[code]) {
            const [title, body, action] = TEXT[code];
            return {title, body, action};
        }
    }
    const [title, body, action] = TEXT.en;
    return {title, body, action};
}

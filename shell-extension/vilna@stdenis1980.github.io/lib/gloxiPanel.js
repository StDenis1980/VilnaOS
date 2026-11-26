// SPDX-License-Identifier: GPL-3.0-or-later
//
// The Gloxi side panel (stage B of the Gloxi window), pure logic: what the
// panel shows from gloxid's events (gloxi/docs/service-protocol.md), the
// owl's state, the requests it sends and its words in Vilna's ten
// languages. gloxiPanel.js draws it; nothing here touches the Shell.

// Kept on screen at most; older lines leave the panel (not the history).
export const MAX_MESSAGES = 200;
// A line typed in the panel is at most this long (gloxid takes more, the
// panel is for short asks; longer text belongs in the window).
export const MAX_INPUT = 4000;
// One answer shown at most this long; the window has the rest.
const MAX_TEXT = 20000;
const MAX_FIELD = 300;

// The owl's states as Dmytro Sizmin drew them (docs/design/owl-states/):
// the panel's state → the picture.
export const GLOXI_APP_ID = 'io.github.stdenis1980.Gloxi';

/** Whether a notification source's app id (with or without .desktop) is Gloxi's. */
export function isGloxiSource(appId) {
    return typeof appId === 'string' && appId.replace(/\.desktop$/, '') === GLOXI_APP_ID;
}

export const OWL_PICTURES = {
    idle: 'waiting',
    thinking: 'thinking',
    answering: 'speaking',
    error: 'error',
    offline: 'error',
};

export function owlFile(state, dark) {
    const picture = OWL_PICTURES[state] ?? 'waiting';
    return `gloxi-owl-${picture}-${dark ? 'dark' : 'light'}.svg`;
}

function text(value, max = MAX_FIELD) {
    const s = typeof value === 'string' ? value : '';
    return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
}

/** One request line for gloxid. */
export function requestLine(id, method, params = {}) {
    return `${JSON.stringify({id, method, params})}\n`;
}

/** The language to say hello with: the first real one, like uk_UA.UTF-8. */
export function helloLanguage(languageNames) {
    for (const name of languageNames ?? []) {
        const s = String(name);
        if (s && s !== 'C' && s !== 'POSIX' && /^[A-Za-z0-9_@.:-]{1,100}$/.test(s))
            return s;
    }
    return null;
}

/** A link the panel may open (as the window: https and mailto only). */
export function safeUri(url) {
    return typeof url === 'string' && /^(https:\/\/|mailto:)/i.test(url) && !/\s/.test(url);
}

/**
 * The conversation as the panel shows it. Lines are
 * {kind: 'user'|'gloxi'|'note'|'error'|'action', text, caption?, status?}.
 * apply() takes one parsed line from gloxid and says whether anything the
 * panel shows changed.
 */
export class PanelChat {
    constructor() {
        this.lines = [];
        this.busy = false;
        this.error = false;
        this.offline = false;
        this._answer = null;
        // For a screen reader: lines to read out, and whether the person
        // asked from this panel (then the answer is read even after they
        // moved away; one asked from the window is read by the window).
        this._news = [];
        this._asking = false;
        this._mine = false;
    }

    get state() {
        if (this.offline)
            return 'offline';
        if (this.error)
            return 'error';
        if (this.busy)
            return this._answer?.text ? 'answering' : 'thinking';
        return 'idle';
    }

    _tell(line, urgent = false, always = false) {
        this._news.push({line, urgent, always});
        if (this._news.length > MAX_MESSAGES)
            this._news.splice(0, this._news.length - MAX_MESSAGES);
    }

    /** The person sent a question from the panel (before chat.submit). */
    asking(on = true) {
        this._asking = on;
    }

    /**
     * What a screen reader should say now, oldest first, and forget it.
     * Gloxi's words, notes and cards: when the panel has the focus or the
     * question came from it. Errors and notifications always: a notification
     * shown here has no banner that would be read instead.
     */
    takeNews(focused) {
        const news = this._news.filter(n => n.always || focused || this._mine)
            .filter(n => n.line.text || n.line.kind === 'action')
            .map(({line, urgent}) => ({line, urgent}));
        this._news = [];
        return news;
    }

    _push(line) {
        this.lines.push(line);
        if (this.lines.length > MAX_MESSAGES)
            this.lines.splice(0, this.lines.length - MAX_MESSAGES);
        return line;
    }

    /**
     * A notification Gloxi sent while the panel is open: it goes into the
     * panel instead of a banner (it stays in the Shell's notification list).
     */
    notice(title, body) {
        const t = text(title), b = text(body, MAX_TEXT);
        if (!t && !b)
            return false;
        this._tell(this._push({kind: 'notice', text: t && b ? `${t}: ${b}` : t || b}), false, true);
        return true;
    }

    /** gloxid's hello: the kept conversation, newest last. */
    loadHistory(history) {
        this.lines = [];
        this._answer = null;
        this._news = [];
        for (const m of Array.isArray(history) ? history.slice(-MAX_MESSAGES) : []) {
            if (m?.role === 'user' || m?.role === 'assistant') {
                const t = text(m.text, MAX_TEXT);
                if (t)
                    this._push({kind: m.role === 'user' ? 'user' : 'gloxi', text: t});
            }
        }
        this.offline = false;
    }

    apply(message) {
        if (!message || typeof message !== 'object' || typeof message.event !== 'string')
            return false;
        switch (message.event) {
        case 'user_message':
            this.error = false;
            this._mine = this._asking;
            this._asking = false;
            this._push({kind: 'user', text: text(message.text, MAX_TEXT)});
            return true;
        case 'start_answer':
            this._answer = this._push({kind: 'gloxi', text: '', caption: text(message.label)});
            return true;
        case 'append_answer':
            if (!this._answer)
                this._answer = this._push({kind: 'gloxi', text: ''});
            this._answer.text = text(this._answer.text + String(message.piece ?? ''), MAX_TEXT);
            return true;
        case 'finish_answer':
            // An answer that never got a word is not left as an empty line.
            if (this._answer && !this._answer.text)
                this.lines.splice(this.lines.indexOf(this._answer), 1);
            else if (this._answer)
                this._tell(this._answer);
            this._answer = null;
            return true;
        case 'busy':
            this.busy = Boolean(message.busy);
            return true;
        case 'note':
            this._tell(this._push({kind: 'note', text: text(message.text)}));
            return true;
        case 'error':
            this.error = true;
            this._tell(this._push({kind: 'error', text: text(message.text)}), true, true);
            return true;
        case 'action': {
            const card = message.card && typeof message.card === 'object' ? message.card : {};
            const status = ['done', 'denied', 'error'].includes(card.status) ? card.status : 'error';
            const program = text(card.program), operation = text(card.operation);
            const title = program && operation ? `${program}: ${operation}` : program || operation;
            const warned = Array.isArray(card.warnings) && card.warnings.length > 0;
            this._tell(this._push({kind: 'action', text: title, status, warned}), warned || status !== 'done');
            return true;
        }
        default:
            return false;
        }
    }
}

// The panel's words. English is the key; the rest are Vilna's languages
// (ADR 0009). A missing language shows English.
const KEYS = ['indicator', 'ready', 'thinking', 'answering', 'error', 'offline', 'hint', 'send', 'openWindow',
    'close', 'you', 'empty', 'replaced', 'done', 'denied', 'failed', 'suspicious', 'messages'];

// The table's key for a GLib language name: "uk_UA.UTF-8" → "uk", and
// Valencian, "ca_ES.UTF-8@valencia" → "ca@valencia" (ADR 0009).
function languageCode(name) {
    const s = String(name);
    const code = s.split(/[_.@-]/)[0].toLowerCase();
    return code === 'ca' && /@valencia/i.test(s) ? 'ca@valencia' : code;
}

const TEXT = {
    en: ['Gloxi side panel', 'Gloxi is ready', 'Gloxi is thinking', 'Gloxi is answering', 'Something went wrong',
        'Gloxi is not running here. Open the Gloxi window.', 'Write to Gloxi…', 'Send', 'Open the Gloxi window',
        'Close the panel', 'You', 'How can I help?', 'Gloxi is open in another panel.', 'Done', 'Denied', 'Failed',
        'Suspicious content: see the Gloxi window', 'Conversation with Gloxi'],
    uk: ['Бічна панель Gloxi', 'Gloxi готовий', 'Gloxi думає', 'Gloxi відповідає', 'Щось пішло не так',
        'Gloxi тут не запущено. Відкрийте вікно Gloxi.', 'Напишіть Gloxi…', 'Надіслати', 'Відкрити вікно Gloxi',
        'Закрити панель', 'Ви', 'Чим допомогти?', 'Gloxi відкрито на іншій панелі.', 'Готово', 'Відхилено',
        'Не вдалося', 'Підозрілий вміст: подробиці у вікні Gloxi', 'Розмова з Gloxi'],
    es: ['Panel lateral de Gloxi', 'Gloxi está listo', 'Gloxi está pensando', 'Gloxi está respondiendo',
        'Algo ha salido mal', 'Gloxi no se está ejecutando aquí. Abra la ventana de Gloxi.', 'Escriba a Gloxi…',
        'Enviar', 'Abrir la ventana de Gloxi', 'Cerrar el panel', 'Usted', '¿En qué puedo ayudar?',
        'Gloxi está abierto en otro panel.', 'Hecho', 'Denegado', 'Falló',
        'Contenido sospechoso: vea la ventana de Gloxi', 'Conversación con Gloxi'],
    pl: ['Panel boczny Gloxi', 'Gloxi jest gotowy', 'Gloxi myśli', 'Gloxi odpowiada', 'Coś poszło nie tak',
        'Gloxi nie działa tutaj. Otwórz okno Gloxi.', 'Napisz do Gloxi…', 'Wyślij', 'Otwórz okno Gloxi',
        'Zamknij panel', 'Ty', 'W czym mogę pomóc?', 'Gloxi jest otwarty w innym panelu.', 'Gotowe', 'Odmówiono',
        'Nie udało się', 'Podejrzana treść: szczegóły w oknie Gloxi', 'Rozmowa z Gloxi'],
    de: ['Gloxi-Seitenleiste', 'Gloxi ist bereit', 'Gloxi denkt nach', 'Gloxi antwortet', 'Etwas ist schiefgelaufen',
        'Gloxi läuft hier nicht. Öffnen Sie das Gloxi-Fenster.', 'An Gloxi schreiben…', 'Senden',
        'Gloxi-Fenster öffnen', 'Seitenleiste schließen', 'Sie', 'Wie kann ich helfen?',
        'Gloxi ist in einer anderen Seitenleiste geöffnet.', 'Erledigt', 'Abgelehnt', 'Fehlgeschlagen',
        'Verdächtiger Inhalt: siehe Gloxi-Fenster', 'Gespräch mit Gloxi'],
    fr: ['Panneau latéral de Gloxi', 'Gloxi est prêt', 'Gloxi réfléchit', 'Gloxi répond', 'Un problème est survenu',
        'Gloxi ne fonctionne pas ici. Ouvrez la fenêtre de Gloxi.', 'Écrire à Gloxi…', 'Envoyer',
        'Ouvrir la fenêtre de Gloxi', 'Fermer le panneau', 'Vous', 'Comment puis-je aider ?',
        'Gloxi est ouvert dans un autre panneau.', 'Fait', 'Refusé', 'Échec',
        'Contenu suspect : voir la fenêtre de Gloxi', 'Conversation avec Gloxi'],
    it: ['Pannello laterale di Gloxi', 'Gloxi è pronto', 'Gloxi sta pensando', 'Gloxi sta rispondendo',
        'Qualcosa è andato storto', 'Gloxi non è in esecuzione qui. Apri la finestra di Gloxi.', 'Scrivi a Gloxi…',
        'Invia', 'Apri la finestra di Gloxi', 'Chiudi il pannello', 'Tu', 'Come posso aiutarti?',
        'Gloxi è aperto in un altro pannello.', 'Fatto', 'Negato', 'Non riuscito',
        'Contenuto sospetto: vedi la finestra di Gloxi', 'Conversazione con Gloxi'],
    ro: ['Panoul lateral Gloxi', 'Gloxi este gata', 'Gloxi se gândește', 'Gloxi răspunde', 'Ceva nu a mers bine',
        'Gloxi nu rulează aici. Deschideți fereastra Gloxi.', 'Scrieți-i lui Gloxi…', 'Trimite',
        'Deschide fereastra Gloxi', 'Închide panoul', 'Dvs.', 'Cu ce vă pot ajuta?',
        'Gloxi este deschis în alt panou.', 'Gata', 'Refuzat', 'Nu a reușit',
        'Conținut suspect: vedeți fereastra Gloxi', 'Conversație cu Gloxi'],
    cs: ['Boční panel Gloxi', 'Gloxi je připraven', 'Gloxi přemýšlí', 'Gloxi odpovídá', 'Něco se pokazilo',
        'Gloxi zde neběží. Otevřete okno Gloxi.', 'Napište Gloxi…', 'Odeslat', 'Otevřít okno Gloxi',
        'Zavřít panel', 'Vy', 'S čím mohu pomoci?', 'Gloxi je otevřen v jiném panelu.', 'Hotovo', 'Zamítnuto',
        'Nezdařilo se', 'Podezřelý obsah: podrobnosti v okně Gloxi', 'Rozhovor s Gloxi'],
    lt: ['Gloxi šoninis skydelis', 'Gloxi pasiruošęs', 'Gloxi galvoja', 'Gloxi atsako', 'Kažkas nepavyko',
        'Gloxi čia neveikia. Atverkite Gloxi langą.', 'Parašykite Gloxi…', 'Siųsti', 'Atverti Gloxi langą',
        'Uždaryti skydelį', 'Jūs', 'Kuo galiu padėti?', 'Gloxi atvertas kitame skydelyje.', 'Atlikta', 'Atmesta',
        'Nepavyko', 'Įtartinas turinys: žr. Gloxi langą', 'Pokalbis su Gloxi'],
    ca: ['Plafó lateral de Gloxi', 'Gloxi està a punt', 'Gloxi està pensant', 'Gloxi està responent',
        'Alguna cosa ha anat malament', 'Gloxi no s’està executant aquí. Obre la finestra de Gloxi.',
        'Escriu a Gloxi…', 'Envia', 'Obre la finestra de Gloxi', 'Tanca el plafó', 'Tu', 'En què et puc ajudar?',
        'Gloxi està obert en un altre plafó.', 'Fet', 'Denegat', 'Ha fallat',
        'Contingut sospitós: mira la finestra de Gloxi', 'Conversa amb Gloxi'],
    'ca@valencia': ['Plafó lateral de Gloxi', 'Gloxi està a punt', 'Gloxi està pensant', 'Gloxi està responent',
        'Alguna cosa ha anat malament', 'Gloxi no s’està executant ací. Obre la finestra de Gloxi.',
        'Escriu a Gloxi…', 'Envia', 'Obre la finestra de Gloxi', 'Tanca el plafó', 'Tu', 'En què et puc ajudar?',
        'Gloxi està obert en un altre plafó.', 'Fet', 'Denegat', 'Ha fallat',
        'Contingut sospitós: mira la finestra de Gloxi', 'Conversa amb Gloxi'],
    gl: ['Panel lateral de Gloxi', 'Gloxi está listo', 'Gloxi está a pensar', 'Gloxi está a responder',
        'Algo saíu mal', 'Gloxi non se está a executar aquí. Abre a xanela de Gloxi.', 'Escríbelle a Gloxi…',
        'Enviar', 'Abrir a xanela de Gloxi', 'Pechar o panel', 'Ti', 'En que che podo axudar?',
        'Gloxi está aberto noutro panel.', 'Feito', 'Denegado', 'Fallou',
        'Contido sospeitoso: mira a xanela de Gloxi', 'Conversa con Gloxi'],
    eu: ['Gloxiren alboko panela', 'Gloxi prest dago', 'Gloxi pentsatzen ari da', 'Gloxi erantzuten ari da',
        'Zerbaitek huts egin du', 'Gloxi ez dabil hemen. Ireki Gloxiren leihoa.', 'Idatzi Gloxiri…', 'Bidali',
        'Ireki Gloxiren leihoa', 'Itxi panela', 'Zu', 'Zertan lagun zaitzaket?',
        'Gloxi beste panel batean irekita dago.', 'Eginda', 'Ukatuta', 'Huts egin du',
        'Eduki susmagarria: ikusi Gloxiren leihoa', 'Elkarrizketa Gloxirekin'],
    pt: ['Painel lateral do Gloxi', 'O Gloxi está pronto', 'O Gloxi está a pensar', 'O Gloxi está a responder',
        'Algo correu mal', 'O Gloxi não está a ser executado aqui. Abra a janela do Gloxi.', 'Escreva ao Gloxi…',
        'Enviar', 'Abrir a janela do Gloxi', 'Fechar o painel', 'Você', 'Em que posso ajudar?',
        'O Gloxi está aberto noutro painel.', 'Feito', 'Recusado', 'Falhou',
        'Conteúdo suspeito: veja a janela do Gloxi', 'Conversa com o Gloxi'],
};

export const LANGUAGES = Object.keys(TEXT);

function languageOf(languageNames) {
    for (const name of languageNames ?? []) {
        const code = languageCode(name);
        if (TEXT[code])
            return code;
    }
    return 'en';
}

// languageNames: GLib.get_language_names(), most wanted first.
export function panelText(languageNames) {
    const values = TEXT[languageOf(languageNames)];
    return Object.fromEntries(KEYS.map((key, i) => [key, values[i]]));
}

export function checkTables() {
    return LANGUAGES.filter(code => TEXT[code].length !== KEYS.length);
}

/** What screen readers and the state line say for the owl's state. */
export function stateText(t, state) {
    return {idle: t.ready, thinking: t.thinking, answering: t.answering, error: t.error, offline: t.offline}[state] ??
        t.ready;
}

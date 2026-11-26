// SPDX-License-Identifier: GPL-3.0-or-later
//
// One notification after an automatic rollback (milestone V2).
//
// When an update fails its boot health check, health-checker goes back to
// a snapshot that worked, and vilna-health-last-working (vilna-branding)
// leaves a note in /var/lib/misc/vilna-auto-rollback:
//
//   failed=<snapshot that failed>
//   restored=<snapshot booted instead, or empty>
//   time=<seconds since 1970>
//
// After the next login the Shell tells the person once, so that a rollback
// is not silent. Each person sees it once per failed snapshot (the setting
// rollback-noticed), and an old note is not shown to someone who logs in
// weeks later. Pure logic, no GNOME imports, so that node can test it.

export const NOTE_PATH = '/var/lib/misc/vilna-auto-rollback';
// Older notes are history, not news.
export const MAX_AGE_SECONDS = 14 * 24 * 3600;
// The note is three short lines; anything longer is not ours.
const MAX_NOTE_LENGTH = 4096;

// text: the file's contents. Returns {failed, restored, time} or null.
export function parseNote(text) {
    if (typeof text !== 'string' || text.length > MAX_NOTE_LENGTH)
        return null;
    const fields = {};
    for (const line of text.split('\n')) {
        const m = /^(failed|restored|time)=([0-9]*)$/.exec(line.trim());
        if (m)
            fields[m[1]] = m[2];
    }
    if (!fields.failed || !fields.time)
        return null;
    return {
        failed: fields.failed,
        restored: fields.restored || null,
        time: Number(fields.time),
    };
}

// note: from parseNote; shown: the setting rollback-noticed; now: seconds.
export function shouldShow(note, shown, now) {
    if (!note || note.failed === shown)
        return false;
    const age = now - note.time;
    // A clock before the note (a wrong RTC) still shows it.
    return age <= MAX_AGE_SECONDS;
}

// The words, in the ten languages of Vilna (ADR 0009).
// The table's key for a GLib language name: "uk_UA.UTF-8" → "uk", and
// Valencian, "ca_ES.UTF-8@valencia" → "ca@valencia" (ADR 0009).
function languageCode(name) {
    const s = String(name);
    const code = s.split(/[_.@-]/)[0].toLowerCase();
    return code === 'ca' && /@valencia/i.test(s) ? 'ca@valencia' : code;
}

const TEXT = {
    en: ['Update undone', 'The last update did not start properly, so Vilna went back to a state that worked before. Your files were not changed.'],
    uk: ['Оновлення скасовано', 'Останнє оновлення не запустилося як слід, тому Vilna повернулася до стану, який працював раніше. Ваші файли не змінено.'],
    es: ['Actualización deshecha', 'La última actualización no arrancó bien, así que Vilna volvió a un estado que funcionaba antes. Tus archivos no se han modificado.'],
    pl: ['Aktualizacja cofnięta', 'Ostatnia aktualizacja nie uruchomiła się poprawnie, więc Vilna wróciła do stanu, który wcześniej działał. Twoje pliki nie zostały zmienione.'],
    de: ['Update zurückgenommen', 'Das letzte Update ist nicht richtig gestartet, daher ist Vilna zu einem Zustand zurückgekehrt, der vorher funktioniert hat. Ihre Dateien wurden nicht verändert.'],
    fr: ['Mise à jour annulée', 'La dernière mise à jour n’a pas démarré correctement, Vilna est donc revenu à un état qui fonctionnait avant. Vos fichiers n’ont pas été modifiés.'],
    it: ['Aggiornamento annullato', 'L’ultimo aggiornamento non si è avviato correttamente, quindi Vilna è tornata a uno stato che prima funzionava. I file non sono stati modificati.'],
    ro: ['Actualizare anulată', 'Ultima actualizare nu a pornit corect, așa că Vilna a revenit la o stare care funcționa înainte. Fișierele nu au fost modificate.'],
    cs: ['Aktualizace vrácena', 'Poslední aktualizace se nespustila správně, proto se Vilna vrátila do stavu, který dříve fungoval. Vaše soubory se nezměnily.'],
    lt: ['Atnaujinimas atšauktas', 'Paskutinis atnaujinimas tinkamai nepasileido, todėl Vilna grįžo į anksčiau veikusią būseną. Jūsų failai nepakeisti.'],
    ca: ['Actualització desfeta',
        'L’última actualització no s’ha iniciat bé, així que Vilna ha tornat a un estat que funcionava abans. Els teus fitxers no s’han modificat.'],
    'ca@valencia': ['Actualització desfeta',
        'L’última actualització no s’ha iniciat bé, així que Vilna ha tornat a un estat que funcionava abans. Els teus fitxers no s’han modificat.'],
    gl: ['Actualización desfeita',
        'A última actualización non arrincou ben, así que Vilna volveu a un estado que funcionaba antes. Os teus ficheiros non se modificaron.'],
    eu: ['Eguneratzea desegin da',
        'Azken eguneratzea ez da ondo abiarazi; beraz, Vilna lehen funtzionatzen zuen egoera batera itzuli da. Zure fitxategiak ez dira aldatu.'],
    pt: ['Atualização anulada',
        'A última atualização não arrancou corretamente, por isso o Vilna voltou a um estado que funcionava antes. Os seus ficheiros não foram alterados.'],
};

export const LANGUAGES = Object.keys(TEXT);

// languageNames: GLib.get_language_names(), most wanted first.
export function noticeText(languageNames) {
    for (const name of languageNames ?? []) {
        const code = languageCode(name);
        if (TEXT[code]) {
            const [title, body] = TEXT[code];
            return {title, body};
        }
    }
    const [title, body] = TEXT.en;
    return {title, body};
}

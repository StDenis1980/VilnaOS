// SPDX-License-Identifier: GPL-3.0-or-later
//
// Which input source to use in a password field. Pure logic: no GNOME
// imports, so it runs under gjs inside the Shell and under node in tests.
//
// A source is {type, id}: type 'xkb' (id like 'us', 'ua', 'rs+latin') or
// 'ibus' (id is the engine name), as in org.gnome.desktop.input-sources.

// XKB layouts whose default variant does not type Latin letters. A variant
// with "latin" in its name (rs+latin, uz+latin) types Latin letters.
// Everything else (us, gb, pl, de, fr, lt, cz, ro, …) is Latin.
export const NON_LATIN_LAYOUTS = new Set([
    'af', 'am', 'ara', 'bd', 'bg', 'bt', 'by', 'eg', 'et', 'ge', 'gr',
    'il', 'in', 'iq', 'ir', 'kg', 'kh', 'kz', 'la', 'lk', 'mk', 'mm',
    'mn', 'mv', 'np', 'pk', 'rs', 'ru', 'sy', 'th', 'tj', 'ua', 'uz',
]);

/** The key a source is stored under, unique across types. */
export function sourceKey(source) {
    return source ? `${source.type}:${source.id}` : null;
}

/** True when typing in this source gives Latin letters, as a password expects. */
export function isLatin(source) {
    if (!source || source.type !== 'xkb' || typeof source.id !== 'string')
        return false;
    const [layout, variant = ''] = source.id.split('+');
    if (!layout)
        return false;
    if (NON_LATIN_LAYOUTS.has(layout))
        return variant.includes('latin');
    return true;
}

/**
 * The Latin source to switch to: the first of `preferred` (xkb ids, in that
 * order) that is configured, else the first configured Latin source, else
 * null when the person has no Latin layout at all.
 */
export function pickLatin(sources, preferred = []) {
    const list = Array.isArray(sources) ? sources : [];
    for (const id of Array.isArray(preferred) ? preferred : []) {
        const found = list.find(s => s && s.type === 'xkb' && s.id === id);
        if (found)
            return found;
    }
    return list.find(isLatin) ?? null;
}

/**
 * What to do when a password field gets focus.
 *  - {action: 'stay'}: the current source already types Latin letters
 *    (a Polish or German layout is kept, as on macOS);
 *  - {action: 'switch', to}: switch to this source;
 *  - {action: 'no-latin'}: no Latin layout is configured; warn instead.
 */
export function decideOnEnter(sources, current, preferred = []) {
    if (isLatin(current))
        return {action: 'stay'};
    const to = pickLatin(sources, preferred);
    return to ? {action: 'switch', to} : {action: 'no-latin'};
}

/** The source after `current`, wrapping around: the indicator's click. */
export function nextSource(sources, current) {
    const list = Array.isArray(sources) ? sources.filter(Boolean) : [];
    if (list.length === 0)
        return null;
    const key = sourceKey(current);
    const at = list.findIndex(s => sourceKey(s) === key);
    return list[(at + 1) % list.length];
}

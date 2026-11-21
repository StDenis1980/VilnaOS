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

// The themes obs/packages/vilna-sound-themes makes, plus GNOME's default.
export const SOUND_THEMES = ['wood', 'bell', 'water', 'air', 'birds', 'glass', 'freedesktop'];

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

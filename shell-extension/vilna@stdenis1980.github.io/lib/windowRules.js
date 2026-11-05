// SPDX-License-Identifier: GPL-3.0-or-later
//
// Which windows the extension looks after. Pure logic.

export const GLOXI_APP_ID = 'io.github.stdenis1980.Gloxi';

// The Herkos confirmation dialog: on its own, and inside the Gloxi Flatpak,
// where it must sit under Gloxi's id (herkos/src/herkos/dialog.py).
export const HERKOS_DIALOG_IDS = [
    'io.github.stdenis1980.Herkos.Dialog',
    `${GLOXI_APP_ID}.HerkosDialog`,
];

/**
 * 'gloxi', 'herkos-dialog' or null, from the ids a window reports (the
 * Wayland app id, which GNOME exposes as the WM class, and the GTK
 * application id). Matching is exact: the Herkos dialog's id starts with
 * Gloxi's, and it must not be mistaken for the Gloxi window.
 */
export function windowRole(ids) {
    const list = (Array.isArray(ids) ? ids : [])
        .filter(id => typeof id === 'string' && id.length > 0)
        .map(id => id.toLowerCase());
    if (HERKOS_DIALOG_IDS.some(id => list.includes(id.toLowerCase())))
        return 'herkos-dialog';
    if (list.includes(GLOXI_APP_ID.toLowerCase()))
        return 'gloxi';
    return null;
}

/**
 * Whether a window should be kept above all others.
 * The Herkos dialog always is: Gloxi above everything must never hide the
 * question Herkos is asking. Gloxi follows the person's setting.
 * Only top-level windows count; Gloxi's own dialogs follow their parent.
 */
export function wantsAbove(role, {gloxiKeepAbove = true, transient = false} = {}) {
    if (transient)
        return false;
    if (role === 'herkos-dialog')
        return true;
    if (role === 'gloxi')
        return Boolean(gloxiKeepAbove);
    return false;
}

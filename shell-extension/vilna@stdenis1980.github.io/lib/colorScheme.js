// SPDX-License-Identifier: GPL-3.0-or-later
//
// Which Vilna style class the Shell gets for the person's style setting
// (org.gnome.desktop.interface color-scheme). Vilna is light by default
// (decision of 2026-11-09), so only 'prefer-dark' gives the dark one.
// Pure logic, no GNOME imports, so that node can test it.

export const LIGHT = 'vilna-light';
export const DARK = 'vilna-dark';

export function schemeClass(colorScheme) {
    return colorScheme === 'prefer-dark' ? DARK : LIGHT;
}

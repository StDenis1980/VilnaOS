/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Vilna: search the store with the text a person meant when the keyboard
 * was in another layout ("ашкуащч" typed for "firefox"). Added to GNOME
 * Software by Vilna (obs/packages/gnome-software in the VilnaOS repository).
 */

#pragma once

#include <glib.h>

G_BEGIN_DECLS

/* The same key presses read in the person's other layouts: a NULL-terminated
 * list, nearest first, never NULL, without @text itself. Layouts are xkb
 * names as in org.gnome.desktop.input-sources ("ua", "de+nodeadkeys"); "us"
 * is tried even when it is not among them. A candidate is made only when
 * every letter of @text is on the keyboard of the layout it was typed in.
 * Main thread only (the keymaps are cached). */
gchar **vilna_layout_search_alternatives_for (const gchar        *text,
                                              const gchar * const *layouts);

/* As above, with the layouts from org.gnome.desktop.input-sources. */
gchar **vilna_layout_search_alternatives     (const gchar        *text);

G_END_DECLS

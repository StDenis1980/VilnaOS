/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Vilna: search the store with the text a person meant when the keyboard
 * was in another layout. See vilna-layout-search.h.
 *
 * No table of letters is written here: xkbcommon compiles each layout from
 * xkeyboard-config, the same data the desktop types with, so every layout a
 * person can add (uk, pl, de, cs, lt, fr, el and the rest) works the same.
 * A character is mapped to the key and shift level that types it in one
 * layout, and back to the character on that key and level in another.
 */

#include "vilna-layout-search.h"

#include <gio/gio.h>
#include <xkbcommon/xkbcommon.h>

#define VILNA_LAYOUT_MAX_LAYOUTS	4
#define VILNA_LAYOUT_MAX_CANDIDATES	6

typedef struct {
	GHashTable *char_to_key;	/* gunichar → key * 2 + level, plus one */
	GHashTable *key_to_char;	/* key * 2 + level → gunichar */
} LayoutMap;

static void
layout_map_free (gpointer data)
{
	LayoutMap *map = data;

	g_hash_table_unref (map->char_to_key);
	g_hash_table_unref (map->key_to_char);
	g_free (map);
}

/* "de+nodeadkeys" → layout "de", variant "nodeadkeys". NULL when xkb does
 * not know the layout. */
static LayoutMap *
layout_map_new (struct xkb_context *context, const gchar *name)
{
	g_auto(GStrv) parts = g_strsplit (name, "+", 2);
	struct xkb_rule_names names = { 0, };
	struct xkb_keymap *keymap;
	LayoutMap *map;

	names.layout = parts[0];
	names.variant = parts[1];
	keymap = xkb_keymap_new_from_names (context, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
	if (keymap == NULL)
		return NULL;

	map = g_new0 (LayoutMap, 1);
	map->char_to_key = g_hash_table_new (NULL, NULL);
	map->key_to_char = g_hash_table_new (NULL, NULL);

	/* Keys in ascending order, so a character on both the main block and
	 * the keypad ("1", ".") belongs to the main block. */
	for (xkb_keycode_t key = xkb_keymap_min_keycode (keymap);
	     key <= xkb_keymap_max_keycode (keymap); key++) {
		for (xkb_level_index_t level = 0; level < 2; level++) {
			const xkb_keysym_t *syms = NULL;
			guint position = key * 2 + level;
			gunichar ch;

			if (xkb_keymap_key_get_syms_by_level (keymap, key, 0, level, &syms) != 1)
				continue;
			ch = xkb_keysym_to_utf32 (syms[0]);
			if (ch == 0 || g_unichar_iscntrl (ch))
				continue;
			g_hash_table_insert (map->key_to_char,
					     GUINT_TO_POINTER (position), GUINT_TO_POINTER (ch));
			if (!g_hash_table_contains (map->char_to_key, GUINT_TO_POINTER (ch)))
				g_hash_table_insert (map->char_to_key, GUINT_TO_POINTER (ch),
						     GUINT_TO_POINTER (position + 1));
		}
	}
	xkb_keymap_unref (keymap);
	return map;
}

/* Cached for the life of the program: a layout compiles in milliseconds,
 * but a search runs on each pause in typing. A failed layout is cached as
 * NULL too. */
static LayoutMap *
layout_map_get (const gchar *name)
{
	static struct xkb_context *context = NULL;
	static GHashTable *cache = NULL;
	LayoutMap *map;

	if (cache == NULL)
		cache = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, layout_map_free);
	if (g_hash_table_lookup_extended (cache, name, NULL, (gpointer *) &map))
		return map;

	if (context == NULL) {
		context = xkb_context_new (XKB_CONTEXT_NO_FLAGS);
		/* A layout xkb does not know is skipped, not a warning. */
		if (context != NULL)
			xkb_context_set_log_level (context, XKB_LOG_LEVEL_CRITICAL);
	}
	map = context != NULL ? layout_map_new (context, name) : NULL;
	g_hash_table_insert (cache, g_strdup (name), map);
	return map;
}

/* @text as typed in @from, read as @to. NULL when a letter of @text is not
 * on @from's keyboard: then it was not typed in @from. Characters that are
 * not letters and are missing from @from (an emoji) are kept. */
static gchar *
convert (const gchar *text, const LayoutMap *from, const LayoutMap *to)
{
	g_autoptr(GString) out = g_string_new (NULL);

	for (const gchar *p = text; *p != '\0'; p = g_utf8_next_char (p)) {
		gunichar ch = g_utf8_get_char (p);
		guint position = GPOINTER_TO_UINT (g_hash_table_lookup (from->char_to_key,
									GUINT_TO_POINTER (ch)));
		gunichar mapped = 0;

		if (position == 0) {
			if (g_unichar_isalpha (ch))
				return NULL;
			g_string_append_unichar (out, ch);
			continue;
		}
		mapped = GPOINTER_TO_UINT (g_hash_table_lookup (to->key_to_char,
								GUINT_TO_POINTER (position - 1)));
		g_string_append_unichar (out, mapped != 0 ? mapped : ch);
	}
	return g_string_free (g_steal_pointer (&out), FALSE);
}

static gboolean
in_list (GPtrArray *list, const gchar *name)
{
	for (guint i = 0; i < list->len; i++) {
		if (g_strcmp0 (g_ptr_array_index (list, i), name) == 0)
			return TRUE;
	}
	return FALSE;
}

gchar **
vilna_layout_search_alternatives_for (const gchar *text, const gchar * const *layouts)
{
	g_autoptr(GPtrArray) names = g_ptr_array_new ();
	g_autoptr(GPtrArray) result = g_ptr_array_new_with_free_func (g_free);

	if (text == NULL || *text == '\0' || !g_utf8_validate (text, -1, NULL))
		return g_new0 (gchar *, 1);

	/* "us" first: most program names are Latin. */
	g_ptr_array_add (names, (gpointer) "us");
	for (guint i = 0; layouts != NULL && layouts[i] != NULL &&
	     names->len < VILNA_LAYOUT_MAX_LAYOUTS; i++) {
		if (!in_list (names, layouts[i]))
			g_ptr_array_add (names, (gpointer) layouts[i]);
	}

	/* Meant in @to, typed in @from: "us" as the meant layout comes first. */
	for (guint t = 0; t < names->len; t++) {
		LayoutMap *to = layout_map_get (g_ptr_array_index (names, t));

		if (to == NULL)
			continue;
		for (guint f = 0; f < names->len; f++) {
			LayoutMap *from;
			g_autofree gchar *candidate = NULL;

			if (f == t || result->len >= VILNA_LAYOUT_MAX_CANDIDATES)
				continue;
			from = layout_map_get (g_ptr_array_index (names, f));
			if (from == NULL)
				continue;
			candidate = convert (text, from, to);
			if (candidate == NULL || g_strcmp0 (candidate, text) == 0 ||
			    in_list (result, candidate))
				continue;
			g_ptr_array_add (result, g_steal_pointer (&candidate));
		}
	}

	g_ptr_array_add (result, NULL);
	return (gchar **) g_ptr_array_free (g_steal_pointer (&result), FALSE);
}

gchar **
vilna_layout_search_alternatives (const gchar *text)
{
	GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
	g_autoptr(GSettingsSchema) schema = NULL;
	g_autoptr(GSettings) settings = NULL;
	g_autoptr(GVariant) sources = NULL;
	g_autoptr(GPtrArray) layouts = g_ptr_array_new_with_free_func (g_free);
	GVariantIter iter;
	const gchar *type;
	const gchar *id;

	if (source != NULL)
		schema = g_settings_schema_source_lookup (source, "org.gnome.desktop.input-sources", TRUE);
	if (schema != NULL && g_settings_schema_has_key (schema, "sources")) {
		settings = g_settings_new ("org.gnome.desktop.input-sources");
		sources = g_settings_get_value (settings, "sources");
		g_variant_iter_init (&iter, sources);
		while (g_variant_iter_next (&iter, "(&s&s)", &type, &id)) {
			if (g_strcmp0 (type, "xkb") == 0)
				g_ptr_array_add (layouts, g_strdup (id));
		}
	}
	g_ptr_array_add (layouts, NULL);
	return vilna_layout_search_alternatives_for (text, (const gchar * const *) layouts->pdata);
}

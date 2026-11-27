/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Tests for vilna-layout-search.c, run by gnome-software.spec %check against
 * the system's xkeyboard-config.
 */

#include "vilna-layout-search.h"

static gboolean
has (gchar **list, const gchar *text)
{
	return g_strv_contains ((const gchar * const *) list, text);
}

static void
test_cyrillic_typed_for_latin (void)
{
	const gchar *layouts[] = { "us", "ua", NULL };
	g_auto(GStrv) list = vilna_layout_search_alternatives_for ("ашкуащч", layouts);
	g_auto(GStrv) upper = vilna_layout_search_alternatives_for ("Ашкуащч", layouts);
	g_auto(GStrv) spaced = vilna_layout_search_alternatives_for ("мі сщву", layouts);

	g_assert_true (has (list, "firefox"));
	g_assert_cmpstr (list[0], ==, "firefox");
	g_assert_true (has (upper, "Firefox"));
	g_assert_true (has (spaced, "vs code"));
}

static void
test_latin_typed_for_cyrillic (void)
{
	const gchar *layouts[] = { "us", "ua", NULL };
	g_auto(GStrv) list = vilna_layout_search_alternatives_for ("rfkmrekznjh", layouts);

	g_assert_true (has (list, "калькулятор"));
}

static void
test_us_is_tried_without_being_set (void)
{
	const gchar *layouts[] = { "ua", NULL };
	g_auto(GStrv) list = vilna_layout_search_alternatives_for ("ашкуащч", layouts);

	g_assert_true (has (list, "firefox"));
}

static void
test_latin_layouts_differ (void)
{
	const gchar *de[] = { "us", "de", NULL };
	const gchar *pl[] = { "us", "pl", NULL };
	g_auto(GStrv) swapped = vilna_layout_search_alternatives_for ("yed", de);
	g_auto(GStrv) same = vilna_layout_search_alternatives_for ("firefox", pl);

	/* QWERTZ swaps y and z; Polish types Latin letters on the same keys. */
	g_assert_true (has (swapped, "zed"));
	g_assert_false (has (same, "firefox"));
	g_assert_cmpuint (g_strv_length (same), ==, 0);
}

static void
test_nothing_to_try (void)
{
	const gchar *one[] = { "us", NULL };
	const gchar *two[] = { "us", "ua", "zz-not-a-layout", NULL };
	g_auto(GStrv) only_us = vilna_layout_search_alternatives_for ("firefox", one);
	g_auto(GStrv) foreign = vilna_layout_search_alternatives_for ("日本", two);
	g_auto(GStrv) digits = vilna_layout_search_alternatives_for ("2048", two);
	g_auto(GStrv) empty = vilna_layout_search_alternatives_for ("", two);
	g_auto(GStrv) unknown = vilna_layout_search_alternatives_for ("ашкуащч", two);

	g_assert_cmpuint (g_strv_length (only_us), ==, 0);
	g_assert_cmpuint (g_strv_length (foreign), ==, 0);
	g_assert_cmpuint (g_strv_length (digits), ==, 0);
	g_assert_cmpuint (g_strv_length (empty), ==, 0);
	/* An unknown layout is skipped, the others still work. */
	g_assert_true (has (unknown, "firefox"));
}

int
main (int argc, char **argv)
{
	g_test_init (&argc, &argv, NULL);
	g_test_add_func ("/vilna/layout-search/cyrillic-for-latin", test_cyrillic_typed_for_latin);
	g_test_add_func ("/vilna/layout-search/latin-for-cyrillic", test_latin_typed_for_cyrillic);
	g_test_add_func ("/vilna/layout-search/us-always", test_us_is_tried_without_being_set);
	g_test_add_func ("/vilna/layout-search/latin-layouts", test_latin_layouts_differ);
	g_test_add_func ("/vilna/layout-search/nothing-to-try", test_nothing_to_try);
	return g_test_run ();
}

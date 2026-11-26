// SPDX-License-Identifier: GPL-3.0-or-later
//
// A vertical St.BoxLayout on every GNOME Shell the extension supports.
//
// GNOME Shell 48 gave St.BoxLayout an «orientation» property, and Shell 51
// no longer has «vertical» at all: `new St.BoxLayout({vertical: true})`
// throws «No property vertical on StBoxLayout», and that one throw put the
// whole extension in ERROR (VM check of Build12.26, 2026-11-24: no widgets,
// no Gloxi button, no side panel). Shell 46 and 47 (still in metadata.json)
// have only «vertical». So the extension never names either property
// itself: it asks vbox.js, which asks the running Shell once.

export function verticalBoxParams(params, hasOrientation, vertical) {
    const rest = {...(params ?? {})};
    delete rest.vertical;
    delete rest.orientation;
    return hasOrientation ? {...rest, orientation: vertical} : {...rest, vertical: true};
}

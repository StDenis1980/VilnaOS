// SPDX-License-Identifier: GPL-3.0-or-later
//
// Rectangles for the layout indicator and the Gloxi strip. Pure logic.
// Rectangles are {x, y, width, height} in stage pixels.

// Panel widths from the Gloxi window design (1/6 or 1/3 of the screen).
export const DOCK_FRACTIONS = {'one-sixth': 1 / 6, 'one-third': 1 / 3};
export const DOCK_MIN_WIDTH = 320;

function finite(n, fallback = 0) {
    return Number.isFinite(n) ? n : fallback;
}

/** The monitor containing the point, else the first one, else null. */
export function monitorAt(monitors, x, y) {
    const list = Array.isArray(monitors) ? monitors.filter(Boolean) : [];
    return list.find(m => x >= m.x && x < m.x + m.width &&
        y >= m.y && y < m.y + m.height) ?? list[0] ?? null;
}

/**
 * Where the layout indicator goes: just below the text cursor, kept inside
 * the monitor; above the cursor when there is no room below.
 */
export function indicatorPosition(cursor, size, monitor, gap = 6) {
    const cx = finite(cursor?.x), cy = finite(cursor?.y);
    const ch = Math.max(0, finite(cursor?.height));
    const w = Math.max(0, finite(size?.width)), h = Math.max(0, finite(size?.height));
    let x = cx, y = cy + ch + gap;
    if (monitor) {
        const right = monitor.x + monitor.width, bottom = monitor.y + monitor.height;
        if (y + h > bottom)
            y = cy - gap - h;
        x = Math.min(Math.max(x, monitor.x), right - w);
        y = Math.min(Math.max(y, monitor.y), bottom - h);
    }
    return {x: Math.round(x), y: Math.round(y)};
}

/** Width of the Gloxi strip on a monitor of this width. */
export function dockWidth(monitorWidth, size = 'one-sixth') {
    const fraction = DOCK_FRACTIONS[size] ?? DOCK_FRACTIONS['one-sixth'];
    const mw = Math.max(0, finite(monitorWidth));
    // Never wider than half the monitor, whatever the minimum says.
    return Math.round(Math.min(Math.max(mw * fraction, DOCK_MIN_WIDTH), mw / 2));
}

/**
 * The strip reserved for the Gloxi panel at the right edge of a monitor
 * (stage B of the window design): from below the top bar (the work area's
 * top) to the bottom of the monitor. GNOME reads a rectangle touching only
 * the right edge as a right-side strut, so maximised windows and
 * notification banners stay left of it, and the top bar's system menu is not
 * covered. A right strut does not change the work area's top, so the work
 * area may be read before or after the strip is reserved.
 */
export function dockStrip(monitor, workArea, size = 'one-sixth') {
    if (!monitor || !(monitor.width > 0) || !(monitor.height > 0))
        return null;
    const width = dockWidth(monitor.width, size);
    const bottom = monitor.y + monitor.height;
    const top = workArea && workArea.y >= monitor.y && workArea.y < bottom
        ? workArea.y : monitor.y;
    return {x: monitor.x + monitor.width - width, y: top, width, height: bottom - top};
}

// SPDX-License-Identifier: GPL-3.0-or-later
//
// (c) Stage B of the Gloxi window: the side panel. A strip at the right edge
// of a monitor is reserved like a dock, and the Shell draws the Gloxi panel
// in it (gloxiPanel.js). It opens on the monitor the person is working on
// (the focused window's, else the pointer's; the primary one at login) and
// stays there until it is closed or that monitor goes away.
//
// The strip is a Shell actor registered as a strut, the same mechanism as
// the top bar. GNOME then keeps maximised and tiled windows out of it, and
// the notification banners, which are laid out in the work area, move left
// of it by themselves. Other windows a strut leaves where they are, so the
// panel moves those it would cover left of it (narrower only when they do
// not fit) and puts them back when it closes, unless the person has moved,
// resized or maximised them meanwhile. On with gloxi-dock (off by default); opened and closed
// with gloxi-panel-open, which the top-bar button, the panel's own close
// button, Escape, the key gloxi-panel-key (Super+G) and Gloxi itself (over
// D-Bus, GloxiPanelOpen, when its own portal key got the press) change.
//
// The panel is drawn by the Shell, as the design brief says for stage B:
// Mutter keeps every normal window at least 75 px inside the work area
// (constrain_partially_onscreen in constraints.c), so the GTK window of
// Gloxi cannot sit in a strut. Checked in GNOME Shell 46.
//
// While the panel is open, Gloxi's own notifications show in it instead of
// as banners; they stay in the Shell's notification list as always.
//
// A strut on an edge between two monitors works too (Mutter 46): windows on
// that monitor keep out of the strip, those on the next one are not touched.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';

import {clearOfStrip, dockMonitor, dockStrip} from './lib/geometry.js';
import {isGloxiSource, panelText} from './lib/gloxiPanel.js';
import {PanelView, hasKeyFocus} from './gloxiPanel.js';

const SLIDE_MS = 250;

// The top-bar button that opens and closes the panel.
const PanelButton = GObject.registerClass(
class VilnaGloxiPanelButton extends PanelMenu.Button {
    _init(name, onToggle) {
        super._init(0.5, name, true);
        this.accessible_name = name;
        this.add_child(new St.Icon({
            icon_name: 'io.github.stdenis1980.Gloxi-symbolic',
            fallback_icon_name: 'user-available-symbolic',
            style_class: 'system-status-icon',
        }));
        this._onToggle = onToggle;
    }

    // On press, as the Shell's own top-bar menus open.
    vfunc_event(event) {
        const type = event.type();
        if (type === Clutter.EventType.BUTTON_PRESS || type === Clutter.EventType.TOUCH_BEGIN) {
            this._onToggle();
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    vfunc_key_press_event(event) {
        const key = event.get_key_symbol();
        if (key === Clutter.KEY_Return || key === Clutter.KEY_KP_Enter || key === Clutter.KEY_space) {
            this._onToggle();
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }
});

function animate() {
    return St.Settings.get().enable_animations;
}

// Maximised (either way, which tiled is too) or fullscreen: Mutter keeps
// those in the work area itself.
function placedByShell(window) {
    return Boolean(window.maximized_horizontally || window.maximized_vertically ||
        window.is_maximized?.() || window.is_fullscreen());
}

export class Dock {
    // dir: the extension's directory, for the owl pictures.
    constructor(settings, dir) {
        this._settings = settings;
        this._dir = dir;
        this._signals = [];
        this._panel = null;
        this._view = null;
        this._button = null;
        this._returnTo = null;
        this._monitor = null; // the index the open panel is on
        this._starting = true;
        this._keyBound = false;
        // Windows moved out of the strip: window → where it was.
        this._moved = new Map();
    }

    enable() {
        const sync = () => this._sync();
        this._connect(this._settings, 'changed::gloxi-dock', sync);
        this._connect(this._settings, 'changed::gloxi-dock-size', sync);
        this._connect(this._settings, 'changed::gloxi-panel-open', sync);
        this._connect(Main.layoutManager, 'monitors-changed', sync);
        // Moved or resized by the person: theirs again, not put back.
        this._connect(global.display, 'grab-op-begin', (_d, window) => this._moved.delete(window));
        this._connect(Main.messageTray, 'source-added', (_t, source) => this._watchSource(source));
        for (const source of Main.messageTray.getSources())
            this._watchSource(source);
        this._sync();
        // Open at login because it was open before: no focus taken then.
        this._starting = false;
    }

    disable() {
        for (const [object, id] of this._signals)
            object.disconnect(id);
        this._signals = [];
        this._release(false);
        this._syncKey(false);
        this._button?.destroy();
        this._button = null;
    }

    /** The strip's actor, or null. */
    get panel() {
        return this._panel;
    }

    /** What the panel shows, or null; for the headless test. */
    get view() {
        return this._view;
    }

    _connect(object, signal, handler) {
        this._signals.push([object, object.connect(signal, handler)]);
    }

    // Gloxi's notifications: a source of the Gloxi app (GTK notifications,
    // the portal's, or libnotify with Gloxi's desktop entry).
    _watchSource(source) {
        if (!isGloxiSource(source?.policy?.id ?? source?.app?.get_id?.()))
            return;
        // notification-added comes before the tray asks for a banner, so an
        // acknowledged notification gets none.
        const id = source.connect('notification-added', (_s, notification) => {
            if (!this._view)
                return;
            // No banner only once the panel shows it: if showing fails,
            // the banner is the person's way to see it.
            try {
                this._view.notice(notification.title, notification.body ?? notification.bannerBodyText);
                notification.acknowledged = true;
            } catch (e) {
                console.error(`Vilna Gloxi panel: ${e.message}\n${e.stack}`);
            }
        });
        const gone = source.connect('destroy', () => {
            this._signals = this._signals.filter(([o]) => o !== source);
        });
        this._signals.push([source, id], [source, gone]);
    }

    toggle() {
        this._settings.set_boolean('gloxi-panel-open', !this._settings.get_boolean('gloxi-panel-open'));
    }

    close() {
        this._settings.set_boolean('gloxi-panel-open', false);
    }

    _sync() {
        const on = this._settings.get_boolean('gloxi-dock');
        this._syncButton(on);
        const open = on && this._settings.get_boolean('gloxi-panel-open');
        const index = open ? this._monitorIndex() : null;
        const monitor = index === null ? null : Main.layoutManager.monitors[index];
        const strip = monitor
            ? dockStrip(monitor, Main.layoutManager.getWorkAreaForMonitor(index),
                this._settings.get_string('gloxi-dock-size'))
            : null;
        if (strip && this._panel && index !== this._monitor)
            this._release(false); // its monitor went away: open again on another
        this._monitor = strip ? index : null;
        if (strip)
            this._reserve(strip);
        else
            this._release(true);
    }

    // The monitor the panel is on, or the one it opens on now.
    _monitorIndex() {
        const count = Main.layoutManager.monitors.length;
        if (this._panel && this._monitor !== null && this._monitor < count)
            return this._monitor;
        const candidates = this._starting ? []
            : [global.display.focus_window?.get_monitor(), global.display.get_current_monitor()];
        return dockMonitor(candidates, count, Main.layoutManager.primaryIndex);
    }

    _syncKey(on) {
        if (on && !this._keyBound) {
            Main.wm.addKeybinding('gloxi-panel-key', this._settings, Meta.KeyBindingFlags.NONE,
                Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW, () => this.toggle());
            this._keyBound = true;
        } else if (!on && this._keyBound) {
            Main.wm.removeKeybinding('gloxi-panel-key');
            this._keyBound = false;
        }
    }

    _syncButton(on) {
        this._syncKey(on);
        if (on && !this._button) {
            this._button = new PanelButton(panelText(GLib.get_language_names()).indicator, () => this.toggle());
            Main.panel.addToStatusArea('vilna-gloxi-panel', this._button, 0, 'right');
        } else if (!on && this._button) {
            this._button.destroy();
            this._button = null;
        }
    }

    _reserve({x, y, width, height}) {
        const opening = !this._panel;
        if (opening) {
            this._panel = new St.Widget({
                name: 'vilnaGloxiDock',
                style_class: 'vilna-gloxi-dock',
                reactive: true,
                layout_manager: new Clutter.BinLayout(),
            });
            this._view = new PanelView(this._dir, () => this.close());
            this._panel.add_child(this._view.actor);
            Main.layoutManager.addChrome(this._panel, {affectsStruts: true, trackFullscreen: true});
        }
        this._panel.set_position(x, y);
        this._panel.set_size(width, height);
        this._makeRoom({x, y, width, height});
        this._button?.add_style_pseudo_class('checked');
        if (!opening)
            return;
        if (animate()) {
            this._panel.translation_x = width;
            this._panel.ease({translation_x: 0, duration: SLIDE_MS, mode: Clutter.AnimationMode.EASE_OUT_QUINT});
        }
        if (!this._starting) {
            // Opened by the person: ready to type, and back to the window
            // they were in when it closes.
            this._returnTo = global.display.focus_window;
            const view = this._view;
            GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
                if (this._view === view)
                    view.focus();
                return GLib.SOURCE_REMOVE;
            });
        }
    }

    // Windows on the panel's monitor that the strip would cover.
    _makeRoom(strip) {
        const area = Main.layoutManager.getWorkAreaForMonitor(this._monitor);
        for (const actor of global.get_window_actors()) {
            const window = actor.meta_window;
            if (!window || window.get_window_type() !== Meta.WindowType.NORMAL ||
                window.get_monitor() !== this._monitor || placedByShell(window) || !window.allows_move())
                continue;
            const from = window.get_frame_rect();
            const to = clearOfStrip(from, strip, area, window.allows_resize());
            if (!to)
                continue;
            if (!this._moved.has(window))
                this._moved.set(window, {x: from.x, y: from.y, width: from.width, height: from.height});
            window.move_resize_frame(true, to.x, to.y, to.width, to.height);
        }
    }

    _giveBack() {
        const alive = new Set(global.display.list_all_windows());
        for (const [window, from] of this._moved) {
            if (alive.has(window) && !placedByShell(window))
                window.move_resize_frame(true, from.x, from.y, from.width, from.height);
        }
        this._moved.clear();
    }

    _release(slide) {
        if (!this._panel)
            return;
        const panel = this._panel, view = this._view;
        const hadFocus = hasKeyFocus(panel);
        this._panel = null;
        this._view = null;
        this._button?.remove_style_pseudo_class('checked');
        // The strut goes at once: windows take the space back while it slides out.
        Main.layoutManager.untrackChrome(panel);
        this._giveBack();
        const done = () => {
            view.destroy();
            panel.destroy();
        };
        if (slide && animate())
            panel.ease({translation_x: panel.width, duration: SLIDE_MS, mode: Clutter.AnimationMode.EASE_IN_CUBIC, onStopped: done});
        else
            done();
        const back = this._returnTo;
        this._returnTo = null;
        if (hadFocus) {
            global.stage.set_key_focus(null);
            if (back && !back.minimized && global.display.list_all_windows().includes(back))
                back.activate(global.get_current_time());
        }
    }
}

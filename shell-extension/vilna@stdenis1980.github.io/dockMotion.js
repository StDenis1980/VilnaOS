// SPDX-License-Identifier: GPL-3.0-or-later
//
// (і) Springy dock icons (dock-icon-motion = spring): an icon grows a
// little under the pointer, sinks when pressed and springs back when let
// go, breathes while its app is starting, and the other icons sway while
// one is dragged to a new place. Our own motion on the motion tokens
// (lib/dock.js), not a copy of another system's.
//
// Dash to Dock builds and rebuilds its docks; this finds them by their
// names (#dashtodockContainer, #dashtodockBoxContainer) and follows the
// icons as they come and go. It moves only the icon picture
// (.overview-icon) inside its padding, so the dock's size and its
// clipping stay as they are, and nothing changes what a click does.
// With animations off in Settings, or window-motion = reduced, nothing moves.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import Shell from 'gi://Shell';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {iconMotion, pivotFor} from './lib/dock.js';

const DOCK_SCHEMA = 'org.gnome.shell.extensions.dash-to-dock';

function dockSettings() {
    const source = Gio.SettingsSchemaSource.get_default();
    return source?.lookup(DOCK_SCHEMA, true) ? new Gio.Settings({schema_id: DOCK_SCHEMA}) : null;
}

function findByName(actor, name, depth = 6) {
    if (!actor || depth < 0)
        return null;
    if (actor.name === name)
        return actor;
    for (const child of actor.get_children?.() ?? []) {
        const found = findByName(child, name, depth - 1);
        if (found)
            return found;
    }
    return null;
}

export class DockMotion {
    constructor(settings) {
        this._settings = settings;
        this._icons = new Map(); // button -> {target, item, ids, app, appId, state}
        this._boxes = new Map(); // box -> ids
        this._signals = [];
        this._dragging = null;
    }

    enable() {
        this._dock = dockSettings();
        this._signals.push([Main.uiGroup, Main.uiGroup.connect('child-added', (_g, actor) => this._watchDock(actor))]);
        this._signals.push([Main.overview, Main.overview.connect('item-drag-begin', (_o, source) => this._dragBegin(source))]);
        this._signals.push([Main.overview, Main.overview.connect('item-drag-end', () => this._dragEnd())]);
        for (const key of ['dock-icon-motion', 'window-motion'])
            this._signals.push([this._settings, this._settings.connect(`changed::${key}`, () => this._settleAll())]);
        for (const actor of Main.uiGroup.get_children())
            this._watchDock(actor);
    }

    disable() {
        for (const [object, id] of this._signals)
            object.disconnect(id);
        this._signals = [];
        for (const button of [...this._icons.keys()])
            this._forget(button, true);
        for (const [box, ids] of this._boxes)
            ids.forEach(id => box.disconnect(id));
        this._boxes.clear();
        this._dock = null;
        this._dragging = null;
    }

    // For the headless test: what the icon of a button last did.
    stateOf(button) {
        return this._icons.get(button)?.state ?? null;
    }

    _options() {
        return {
            motion: this._settings.get_string('dock-icon-motion'),
            reduced: this._settings.get_string('window-motion') === 'reduced',
            animations: St.Settings.get().enable_animations,
        };
    }

    _side() {
        try {
            return this._dock?.get_string('dock-position') ?? 'BOTTOM';
        } catch {
            return 'BOTTOM';
        }
    }

    _watchDock(container) {
        if (container?.name !== 'dashtodockContainer')
            return;
        // The icons are built after the container: wait for its box.
        const box = findByName(container, 'dashtodockBoxContainer');
        if (!box) {
            const id = container.connect('child-added', () => {
                if (findByName(container, 'dashtodockBoxContainer')) {
                    container.disconnect(id);
                    this._watchDock(container);
                }
            });
            return;
        }
        this._watchBox(box);
        const dashContainer = findByName(container, 'dashtodockDashContainer');
        if (dashContainer && dashContainer !== box)
            this._watchBox(dashContainer, false);
        for (const child of box.get_children())
            if (child instanceof St.BoxLayout)
                this._watchBox(child);
    }

    // box: a row (or column) of dash items; the show-apps item may sit
    // in the outer containers, so those are looked at without recursion.
    _watchBox(box, deep = true) {
        if (this._boxes.has(box))
            return;
        const ids = [
            box.connect('child-added', (_b, item) => {
                if (deep && item instanceof St.BoxLayout && !item.child)
                    this._watchBox(item);
                else
                    this._attach(item);
            }),
            box.connect('destroy', () => this._boxes.delete(box)),
        ];
        this._boxes.set(box, ids);
        for (const item of box.get_children())
            this._attach(item);
    }

    _attach(item) {
        // An app item holds its AppIcon (an St.Button with .icon); the
        // show-apps item has toggleButton and icon of its own.
        const button = item?.toggleButton ?? item?.child;
        const target = item?.toggleButton ? item.icon : item?.child?.icon;
        if (!(button instanceof St.Button) || !(target instanceof Clutter.Actor) || this._icons.has(button))
            return;
        const entry = {target, item, ids: [], app: button.app ?? null, appId: 0, state: 'still'};
        entry.ids.push(button.connect('notify::hover', () => this._play(button, button.hover ? 'hover' : 'leave')));
        entry.ids.push(button.connect('notify::pressed', () => {
            if (button.pressed)
                this._play(button, 'press');
            else
                this._play(button, button.hover ? 'release' : 'leave');
        }));
        entry.ids.push(button.connect('destroy', () => this._forget(button, false)));
        if (entry.app instanceof Shell.App)
            entry.appId = entry.app.connect('notify::state', () => this._appState(button));
        this._icons.set(button, entry);
        if (this._dragging && this._dragging !== button)
            this._play(button, 'jiggle');
    }

    _forget(button, settle) {
        const entry = this._icons.get(button);
        if (!entry)
            return;
        if (settle)
            this._apply(entry, iconMotion('still', {motion: 'off'}), 'still');
        for (const id of entry.ids)
            button.disconnect(id);
        if (entry.appId)
            entry.app.disconnect(entry.appId);
        this._icons.delete(button);
    }

    _appState(button) {
        const entry = this._icons.get(button);
        if (!entry)
            return;
        if (entry.app.state === Shell.AppState.STARTING)
            this._play(button, 'pulse');
        else if (entry.state === 'pulse')
            this._play(button, button.hover ? 'hover' : 'still');
    }

    _dragBegin(source) {
        // source: the dragged AppIcon (GNOME's dash and Dash to Dock pass it).
        this._dragging = source ?? true;
        for (const button of this._icons.keys())
            if (button !== source)
                this._play(button, 'jiggle');
    }

    _dragEnd() {
        this._dragging = null;
        for (const [button, entry] of this._icons)
            if (entry.state === 'jiggle')
                this._play(button, 'still');
    }

    _settleAll() {
        for (const [button, entry] of this._icons)
            this._apply(entry, iconMotion('still', {motion: 'off'}), 'still');
    }

    _play(button, event) {
        const entry = this._icons.get(button);
        if (!entry)
            return;
        // While others sway, hovering one does not stop it.
        if (this._dragging && entry.state === 'jiggle' && event !== 'still')
            return;
        const motion = iconMotion(event, this._options());
        if (!motion)
            return;
        this._apply(entry, motion, event);
    }

    _apply(entry, motion, event) {
        const {target} = entry;
        entry.state = event;
        target.remove_all_transitions();
        if (event === 'jiggle') {
            // Sway about the middle, each icon a little out of step.
            target.set_pivot_point(0.5, 0.5);
            target.rotation_angle_z = -motion.rotation;
        } else {
            target.set_pivot_point(...pivotFor(this._side()));
        }
        const mode = Clutter.AnimationMode[motion.mode] ?? Clutter.AnimationMode.EASE_OUT_QUAD;
        if (!motion.duration) {
            target.set({scale_x: motion.scale, scale_y: motion.scale, rotation_angle_z: motion.rotation});
            return;
        }
        const params = {
            scale_x: motion.scale,
            scale_y: motion.scale,
            rotation_angle_z: event === 'jiggle' ? motion.rotation : 0,
            duration: motion.duration,
            mode,
        };
        if (motion.repeat)
            params.repeatCount = motion.repeat;
        if (motion.autoReverse)
            params.autoReverse = true;
        if (event === 'jiggle')
            params.delay = Math.floor(Math.random() * motion.duration);
        if (event === 'pulse')
            params.onComplete = () => {
                if (entry.state === 'pulse')
                    this._apply(entry, iconMotion('still', this._options()) ?? iconMotion('still', {motion: 'off'}), 'still');
            };
        target.ease(params);
    }
}

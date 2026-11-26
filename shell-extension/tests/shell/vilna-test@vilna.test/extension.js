// SPDX-License-Identifier: GPL-3.0-or-later
//
// Test helper for tests/shell/run.sh: runs inside a headless GNOME Shell
// next to the Vilna extension, drives it, and writes PASS/FAIL lines to
// $VILNA_TEST_OUT. Never installed outside the test.

import Atk from 'gi://Atk';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import * as Keyboard from 'resource:///org/gnome/shell/ui/status/keyboard.js';

const UUID = 'vilna@stdenis1980.github.io';
const ENABLED = 1; // ExtensionState.ENABLED
const GLOXI = 'io.github.stdenis1980.Gloxi';
const HERKOS = 'io.github.stdenis1980.Gloxi.HerkosDialog';
const OTHER = 'org.example.Other';

const lines = [];
const sleep = ms => new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
    resolve();
    return GLib.SOURCE_REMOVE;
}));

function check(name, got, want) {
    const ok = JSON.stringify(got) === JSON.stringify(want);
    lines.push(`${ok ? 'PASS' : 'FAIL'} ${name}: got ${JSON.stringify(got)}${ok ? '' : `, want ${JSON.stringify(want)}`}`);
}

function flush() {
    const out = GLib.getenv('VILNA_TEST_OUT');
    if (out)
        GLib.file_set_contents(out, `${lines.join('\n')}\nEND\n`);
}

// Every ease on a window actor, by WM class: what the window motion module
// handed to Clutter (Shell's own path ends in the same prototype method).
const eases = {};
function spyEases() {
    const ease = Clutter.Actor.prototype.ease;
    Clutter.Actor.prototype.ease = function (params) {
        const cls = this instanceof Meta.WindowActor ? this.meta_window?.get_wm_class() : null;
        if (cls)
            (eases[cls] ??= []).push([params.duration, params.mode]);
        return ease.call(this, params);
    };
}
const lastEase = cls => eases[cls]?.at(-1) ?? null;
const Mode = Clutter.AnimationMode;

async function waitFor(predicate, ms = 5000) {
    for (let t = 0; t < ms && !predicate(); t += 100)
        await sleep(100);
    return predicate();
}

// The schema sits in the extension's schemas/ folder (development copy) or
// with the system schemas (the RPM), as Extension.getSettings() looks.
function vilnaSettings() {
    const dir = Main.extensionManager.lookup(UUID).dir.get_child('schemas');
    const source = dir.get_child('gschemas.compiled').query_exists(null)
        ? Gio.SettingsSchemaSource.new_from_directory(
            dir.get_path(), Gio.SettingsSchemaSource.get_default(), false)
        : Gio.SettingsSchemaSource.get_default();
    return new Gio.Settings({settings_schema: source.lookup('org.gnome.shell.extensions.vilna', true)});
}

const manager = () => Keyboard.getInputSourceManager();
const current = () => manager().currentSource?.id ?? null;
const source = id => Object.values(manager().inputSources).find(s => s.id === id);
const indicator = () => Main.layoutManager.uiGroup.get_children()
    .find(a => a.has_style_class_name?.('vilna-layout-indicator'));

async function useLayout(id) {
    source(id).activate(true);
    await sleep(200);
}

async function focus(actor) {
    if (actor)
        actor.grab_key_focus();
    else
        global.stage.set_key_focus(null);
    await sleep(400);
}

async function passwordFields() {
    const entry = new St.PasswordEntry({width: 200});
    const repeat = new St.PasswordEntry({width: 200});
    const plain = new St.Entry({width: 200});
    for (const actor of [entry, repeat, plain])
        Main.layoutManager.addTopChrome(actor);
    entry.set_position(300, 300);
    repeat.set_position(300, 360);
    plain.set_position(300, 420);
    await sleep(300); // allocated and mapped before taking focus
    repeat.set_position(300, 360);
    plain.set_position(300, 420);
    await sleep(300); // allocated and mapped before taking focus

    await useLayout('ua');
    await focus(entry);
    check('password field: purpose', Main.inputMethod.content_purpose, Clutter.InputContentPurpose.PASSWORD);
    check('password field: Latin layout', current(), 'us');
    check('indicator shown with the layout', [indicator()?.visible, indicator()?.label], [true, 'en']);
    await focus(repeat);
    check('repeat-password field keeps Latin', current(), 'us');
    await focus(plain);
    check('plain field: layout given back', current(), 'ua');
    check('indicator hidden', indicator()?.visible ?? false, false);

    await focus(entry);
    await useLayout('pl'); // by hand, inside the field
    await focus(null);
    check('manual switch inside, then leave: original back', current(), 'ua');

    await useLayout('pl');
    await focus(entry);
    check('another Latin layout (Polish) switches to English', current(), 'us');
    await focus(null);
    check('Polish back after the field', current(), 'pl');

    await useLayout('ua');
    await focus(entry);
    const shell = new Gio.Settings({schema_id: 'org.gnome.shell'});
    const enabled = shell.get_strv('enabled-extensions');
    shell.set_strv('enabled-extensions', enabled.filter(u => u !== UUID));
    await waitFor(() => Main.extensionManager.lookup(UUID)?.state !== ENABLED);
    await sleep(300);
    check('disabled inside the field: layout given back', current(), 'ua');
    check('disabled: indicator removed', Boolean(indicator()), false);
    await focus(null);
    shell.set_strv('enabled-extensions', enabled);
    await waitFor(() => Main.extensionManager.lookup(UUID)?.state === ENABLED);
    await useLayout('ua');
    await focus(entry);
    check('enabled again: works', current(), 'us');
    await focus(null);
    for (const actor of [entry, repeat, plain])
        actor.destroy();
}

function nameOwned(name) {
    return new Promise((resolve, reject) => Gio.DBus.session.call('org.freedesktop.DBus',
        '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'NameHasOwner',
        new GLib.Variant('(s)', [name]), null, Gio.DBusCallFlags.NONE, -1, null,
        (connection, result) => {
            try {
                resolve(connection.call_finish(result).deepUnpack()[0]);
            } catch (e) {
                reject(e);
            }
        }));
}

const findWindow = id => global.display.list_all_windows().find(w => w.get_wm_class() === id);
const workArea = () => {
    const a = Main.layoutManager.getWorkAreaForMonitor(Main.layoutManager.primaryIndex);
    return [a.x, a.y, a.width, a.height];
};
const dockPanel = () => {
    const p = Main.layoutManager.uiGroup.get_children().find(a => a.name === 'vilnaGloxiDock');
    return p ? [p.x, p.y, p.width, p.height] : null;
};

function setShellProperty(name, variant) {
    return new Promise((resolve, reject) => Gio.DBus.session.call('io.github.stdenis1980.VilnaShell',
        '/io/github/stdenis1980/VilnaShell', 'org.freedesktop.DBus.Properties', 'Set',
        new GLib.Variant('(ssv)', ['io.github.stdenis1980.VilnaShell1', name, variant]),
        null, Gio.DBusCallFlags.NONE, -1, null, (connection, result) => {
            try {
                connection.call_finish(result);
                resolve(true);
            } catch (e) {
                reject(e);
            }
        }));
}

const frame = w => {
    const r = w.get_frame_rect();
    return [r.x, r.y, r.width, r.height];
};

// Stage A: the Shell keeps the Gloxi window at the right edge.
async function panelPlacement(settings) {
    const gloxi = findWindow(GLOXI);
    const [, top, , height] = workArea();
    const bottom = top + height;
    const tall = h => h - 16;
    await waitFor(() => frame(gloxi)[0] === 1592, 3000);
    check('panel: a sixth at the right edge on first show', frame(gloxi), [1592, top + 8, 320, tall(height)]);
    settings.set_string('gloxi-panel-size', 'compact');
    await sleep(800);
    check('panel: compact at the bottom right', frame(gloxi), [1592, bottom - 8 - 220, 320, 220]);
    await setShellProperty('GloxiPanelSize', new GLib.Variant('s', 'one-third'));
    await sleep(800);
    check('panel: a third, set over D-Bus', [settings.get_string('gloxi-panel-size'), frame(gloxi)],
        ['one-third', [1272, top + 8, 640, tall(height)]]);
    await setShellProperty('GloxiPanelSize', new GLib.Variant('s', 'half')).catch(() => false);
    await sleep(300);
    check('panel: an unknown size over D-Bus is ignored', settings.get_string('gloxi-panel-size'), 'one-third');
    gloxi.move_resize_frame(true, 300, 200, 500, 600);
    await sleep(500);
    settings.set_string('gloxi-panel-size', 'custom');
    await sleep(800);
    check('panel: custom keeps the size, back at the edge', frame(gloxi), [1920 - 8 - 500, top + 8, 500, 600]);
    await setShellProperty('GloxiPanelEdge', new GLib.Variant('b', false));
    await sleep(300);
    gloxi.move_frame(true, 100, 100);
    settings.set_string('gloxi-panel-size', 'one-sixth');
    await sleep(800);
    check('panel: edge off, the window stays where it is', frame(gloxi).slice(0, 2), [100, 100]);
    settings.set_boolean('gloxi-panel-edge', true);
    await sleep(800);
    check('panel: edge on again', frame(gloxi), [1592, top + 8, 320, tall(height)]);
}

async function windows() {
    const settings = vilnaSettings();
    GLib.file_set_contents(`${GLib.getenv('VILNA_TEST_OUT')}.windows`, '');
    const ready = await waitFor(() => [GLOXI, HERKOS, OTHER].every(findWindow), 15000);
    check('test windows appeared', ready, true);
    if (!ready)
        return;
    await sleep(500);
    check('motion: Gloxi opens in 280 ms', eases[GLOXI]?.[0] ?? null, [280, Mode.EASE_OUT_QUINT]);
    check('motion: other window opens in 280 ms', eases[OTHER]?.[0] ?? null, [280, Mode.EASE_OUT_QUINT]);

    // GTK password field (Wayland text-input): each window holds one
    // focused field, a password one in OTHER and a plain one in Gloxi.
    // Start from the plain field: the windows come up in any order, and if
    // OTHER is focused first, choosing 'ua' there is a switch by hand inside
    // the password field, which the extension rightly keeps.
    findWindow(GLOXI).activate(global.get_current_time());
    await waitFor(() => !(indicator()?.visible ?? false), 5000);
    await useLayout('ua');
    findWindow(OTHER).activate(global.get_current_time());
    await waitFor(() => current() === 'us', 5000);
    check('GTK password field: focused window', global.display.focus_window?.get_wm_class() ?? null, OTHER);
    check('GTK password field: Latin layout', current(), 'us');
    check('GTK password field: indicator shown', indicator()?.visible ?? false, true);
    findWindow(GLOXI).activate(global.get_current_time());
    await waitFor(() => current() === 'ua', 2000);
    check('GTK plain field: layout given back', current(), 'ua');

    check('Gloxi above', findWindow(GLOXI).is_above(), true);
    check('Herkos dialog above', findWindow(HERKOS).is_above(), true);
    check('other window not above', findWindow(OTHER).is_above(), false);
    settings.set_boolean('gloxi-keep-above', false);
    await sleep(300);
    check('setting off: Gloxi not above', findWindow(GLOXI).is_above(), false);
    check('setting off: Herkos dialog still above', findWindow(HERKOS).is_above(), true);
    settings.set_boolean('gloxi-keep-above', true);
    await sleep(300);
    check('setting on again: Gloxi above', findWindow(GLOXI).is_above(), true);

    // The other window: a drag lift, then each motion style.
    const otherActor = findWindow(OTHER).get_compositor_private();
    global.display.emit('grab-op-begin', findWindow(OTHER), Meta.GrabOp.MOVING);
    check('motion: drag lifts the window', lastEase(OTHER), [150, Mode.EASE_OUT_QUAD]);
    await sleep(400);
    check('motion: lifted by 1%', otherActor.scale_x, 1.01);
    global.display.emit('grab-op-end', findWindow(OTHER), Meta.GrabOp.MOVING);
    await sleep(400);
    check('motion: set down after the drag', [otherActor.scale_x, otherActor.pivot_point.x], [1, 0]);
    // Rubber window: the far side lags while the window is dragged and
    // springs back afterwards. GNOME Shell turns animations off on
    // software rendering, as here; turn them on for this part only.
    const softwareOnly = !St.Settings.get().enable_animations;
    if (softwareOnly)
        St.Settings.get().uninhibit_animations();
    const wobble = () => otherActor.get_effect('vilna-wobble');
    const other0 = findWindow(OTHER).get_frame_rect();
    global.display.emit('grab-op-begin', findWindow(OTHER), Meta.GrabOp.MOVING);
    check('rubber: effect on while dragged', Boolean(wobble()), true);
    findWindow(OTHER).move_frame(true, other0.x + 160, other0.y + 40);
    await sleep(30);
    const bend = wobble()?.jelly.at(1, 1) ?? [0, 0];
    check('rubber: the window bends with the drag', Math.hypot(...bend) > 2, true);
    global.display.emit('grab-op-end', findWindow(OTHER), Meta.GrabOp.MOVING);
    check('rubber: effect gone once settled', await waitFor(() => !wobble(), 3000), true);
    findWindow(OTHER).move_frame(true, other0.x, other0.y);
    settings.set_boolean('window-wobble', false);
    global.display.emit('grab-op-begin', findWindow(OTHER), Meta.GrabOp.MOVING);
    check('rubber: off by the setting', Boolean(wobble()), false);
    global.display.emit('grab-op-end', findWindow(OTHER), Meta.GrabOp.MOVING);
    settings.set_boolean('window-wobble', true);
    if (softwareOnly) {
        St.Settings.get().inhibit_animations();
        global.display.emit('grab-op-begin', findWindow(OTHER), Meta.GrabOp.MOVING);
        check('rubber: none with animations off', Boolean(wobble()), false);
        global.display.emit('grab-op-end', findWindow(OTHER), Meta.GrabOp.MOVING);
    }
    await sleep(400);

    settings.set_string('window-motion', 'reduced');
    findWindow(OTHER).minimize();
    await sleep(600);
    check('motion: reduced minimise is a 100 ms fade', lastEase(OTHER), [100, Mode.EASE_OUT_QUAD]);
    findWindow(OTHER).unminimize();
    await sleep(600);
    check('motion: reduced unminimise settles', [lastEase(OTHER), otherActor.opacity, otherActor.scale_x],
        [[100, Mode.EASE_OUT_QUAD], 255, 1]);
    settings.set_string('window-motion', 'gnome');
    findWindow(OTHER).minimize();
    await sleep(800);
    check('motion: gnome style keeps the Shell\'s 400 ms', lastEase(OTHER), [400, Mode.EASE_OUT_EXPO]);
    findWindow(OTHER).unminimize();
    await sleep(800);
    settings.set_string('window-motion', 'vilna');
    findWindow(GLOXI).activate(global.get_current_time());
    await sleep(300);

    check('D-Bus name owned in the user session', await nameOwned('io.github.stdenis1980.VilnaShell'), true);
    await panelPlacement(settings);

    const [mx, mw, mh] = [0, 1920, 1080];
    check('monitor is 1920x1080', [Main.layoutManager.primaryMonitor.width, Main.layoutManager.primaryMonitor.height], [mw, mh]);
    const top = workArea()[1];
    await sidePanel(settings, [mx, mw, mh, top]);
    findWindow(GLOXI).minimize();
    await sleep(800);
    check('motion: minimise in 450 ms', lastEase(GLOXI), [450, Mode.EASE_IN_OUT_CUBIC]);
    findWindow(GLOXI).unminimize();
    await sleep(800);
    check('motion: unminimise in 450 ms', lastEase(GLOXI), [450, Mode.EASE_IN_OUT_CUBIC]);
    check('motion: shown window settled', [findWindow(GLOXI).get_compositor_private()?.opacity,
        findWindow(GLOXI).get_compositor_private()?.scale_x], [255, 1]);
    findWindow(GLOXI).delete(global.get_current_time());
    await sleep(1500);
    check('motion: close in 196 ms', lastEase(GLOXI), [196, Mode.EASE_IN_CUBIC]);
}

const theDock = () => Main.extensionManager.lookup(UUID)?.stateObj?._userParts
    ?.find(p => p.constructor.name === 'Dock') ?? null;

// A whole-screen picture for the design review, when VILNA_TEST_SHOTS names
// a folder (tests/shell/run.sh passes it on).
async function shot(name) {
    const dir = GLib.getenv('VILNA_TEST_SHOTS');
    if (!dir)
        return;
    await sleep(600);
    const file = Gio.File.new_for_path(`${dir}/${name}.png`);
    const stream = file.replace(null, false, Gio.FileCreateFlags.NONE, null);
    const shooter = new Shell.Screenshot();
    await new Promise(resolve => shooter.screenshot(false, stream, (s, result) => {
        try {
            s.screenshot_finish(result);
        } catch (e) {
            console.warn(`vilna test: no screenshot: ${e.message}`);
        }
        stream.close(null);
        resolve();
    }));
}

// Stage B: the side panel the Shell draws in the strip, talking to gloxid.
async function sidePanel(settings, [mx, mw, mh, top]) {
    const seat = Clutter.get_default_backend().get_default_seat();
    const keyboard = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
    const pointer = seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
    const press = async (...keys) => {
        for (const k of keys)
            keyboard.notify_keyval(GLib.get_monotonic_time(), k, Clutter.KeyState.PRESSED);
        for (const k of [...keys].reverse())
            keyboard.notify_keyval(GLib.get_monotonic_time(), k, Clutter.KeyState.RELEASED);
        await sleep(400);
    };
    const click = async actor => {
        const [x, y] = actor.get_transformed_position();
        const [w, h] = actor.get_transformed_size();
        pointer.notify_absolute_motion(GLib.get_monotonic_time(), x + w / 2, y + h / 2);
        await sleep(300);
        const picked = () => global.stage.get_actor_at_pos(Clutter.PickMode.REACTIVE, x + w / 2, y + h / 2);
        if (!await waitFor(() => actor.contains(picked()), 3000))
            lines.push(`NOTE click: under the pointer ${picked()?.constructor?.name} ${picked()?.name} ${picked()?.style_class}, panel open ${theDock()?.panel !== null}`);
        pointer.notify_button(GLib.get_monotonic_time(), Clutter.BUTTON_PRIMARY, Clutter.ButtonState.PRESSED);
        await sleep(100);
        pointer.notify_button(GLib.get_monotonic_time(), Clutter.BUTTON_PRIMARY, Clutter.ButtonState.RELEASED);
        await sleep(600);
    };
    const panelFocused = () => {
        const focus = global.stage.get_key_focus();
        return Boolean(focus && theDock()?.panel?.contains(focus));
    };
    const dock = theDock();

    settings.set_boolean('gloxi-dock', true);
    await sleep(500);
    check('side panel: closed until opened', [dockPanel(), workArea()[2]], [null, mw]);
    const button = Main.panel.statusArea['vilna-gloxi-panel'];
    check('side panel: a button in the top bar with a name', Boolean(button?.accessible_name), true);

    findWindow(OTHER).activate(global.get_current_time());
    await sleep(300);
    await press(Clutter.KEY_Super_L, Clutter.KEY_g);
    await sleep(600);
    check('side panel: Super+G opens it', settings.get_boolean('gloxi-panel-open'), true);
    check('strip: work area without it', workArea(), [mx, top, 1600, mh - top]);
    check('strip: panel below the top bar', dockPanel(), [1600, top, 320, mh - top]);
    const view = dock.view;
    check('side panel: said hello to gloxid as the panel', panelRequests[0]?.params?.client ?? null, 'panel');
    check('side panel: the kept conversation, only what was said',
        await waitFor(() => view.chat.lines.length === 2, 3000) && view.chat.lines.map(l => `${l.kind}: ${l.text}`),
        ['user: Що сьогодні?', 'gloxi: О 15:00 зустріч з Дмитром.']);
    check('side panel: ready to type', panelFocused(), true);
    check('side panel: the field\'s name starts with what it shows',
        Boolean(view.entry.accessible_name) && view.entry.hint_text.startsWith(view.entry.accessible_name), true);

    // What Orca would read: the list's ATK notifications (AT-SPI Announcement).
    const heard = [];
    const listAccessible = view._list.get_accessible();
    const hearing = listAccessible.connect('notification', (_a, words, politeness) => heard.push([words, politeness]));
    view.entry.set_text('Увімкни темну тему');
    await press(Clutter.KEY_Return);
    check('side panel: Enter sends, the answer streams in',
        await waitFor(() => view.chat.lines.length === 5 && !view.chat.busy, 3000) &&
            view.chat.lines.slice(2).map(l => `${l.kind}: ${l.text}`),
        ['user: Увімкни темну тему', 'gloxi: Готово: темну тему ввімкнено.', 'action: Settings: dark theme']);
    check('side panel: the field is empty again', view.entry.get_text(), '');
    // The field grows with long text, up to a few lines, and Shift+Enter
    // starts a new line instead of sending (Build12.29: one short line).
    const oneLine = view.fieldHeight;
    view.entry.set_text('Нагадай мені завтра зранку '.repeat(3).trim());
    await sleep(300);
    const grown = view.fieldHeight;
    view.entry.set_text('рядок\n'.repeat(60).trim());
    await sleep(300);
    const tall = view.fieldHeight;
    check(`side panel: the field grows with long text, then stops growing (${oneLine}, ${grown}, ${tall} px)`,
        [grown > oneLine * 1.5, tall > grown, tall < 400], [true, true, true]);
    view.entry.set_text('перший рядок');
    view.entry.clutter_text.set_cursor_position(-1);
    const sent = panelRequests.filter(r => r.method === 'chat.submit').length;
    await press(Clutter.KEY_Shift_L, Clutter.KEY_Return);
    check('side panel: Shift+Enter starts a new line and sends nothing',
        [view.entry.get_text(), panelRequests.filter(r => r.method === 'chat.submit').length],
        ['перший рядок\n', sent]);
    view.entry.set_text('');
    await sleep(300);
    check('side panel: empty, the field is one line again', view.fieldHeight, oneLine);
    // The card as soon as the action is done, the answer once it is finished.
    check('side panel: a screen reader hears the card, then the whole answer once',
        heard.map(([w]) => w), ['Settings: dark theme · Done', 'Gloxi: Готово: темну тему ввімкнено.']);
    check('side panel: politely', heard.every(([, p]) => p === Atk.Live.POLITE), true);
    check('side panel: the owl is ready again, and says so', [view.chat.state, Boolean(view.actor.accessible_description)],
        ['idle', true]);
    check('side panel: the owl picture is there', view._owl.gicon?.get_file?.().query_exists(null) ?? false, true);

    const other = findWindow(OTHER);
    if (Meta.MaximizeFlags)
        other.maximize(Meta.MaximizeFlags.BOTH); // GNOME 46-48
    else
        other.maximize();
    await sleep(800);
    const rect = other.get_frame_rect();
    check('strip: maximised window stays left of it', [rect.x, rect.width], [0, 1600]);
    Main.notify('Vilna test', 'Banner');
    await sleep(800);
    check('strip: banners laid out left of it', Main.messageTray.width, 1600);
    await shot('gloxi-side-panel-light');
    const iface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
    iface.set_string('color-scheme', 'prefer-dark');
    await shot('gloxi-side-panel-dark');
    iface.reset('color-scheme');

    // Gloxi's own notifications: in the panel while it is open, no banner.
    const gloxiSource = new MessageTray.Source({
        title: 'Gloxi', policy: new MessageTray.NotificationApplicationPolicy('io.github.stdenis1980.Gloxi'),
    });
    Main.messageTray.add(gloxiSource);
    const reminder = new MessageTray.Notification({source: gloxiSource, title: 'Нагадування', body: 'Зустріч о 15:00'});
    gloxiSource.addNotification(reminder);
    await sleep(300);
    check('side panel: a Gloxi notification shows in it', view.chat.lines.at(-1), {kind: 'notice', text: 'Нагадування: Зустріч о 15:00'});
    check('side panel: and gets no banner', [reminder.acknowledged, Main.messageTray._notification === reminder,
        Main.messageTray._notificationQueue?.includes(reminder) ?? false], [true, false, false]);
    check('side panel: it stays in the notification list', gloxiSource.notifications.includes(reminder), true);
    check('side panel: a screen reader hears it instead of the banner', heard.at(-1)?.[0], 'Нагадування: Зустріч о 15:00');
    // The same with the focus in a window, not in the panel (Build12.26:
    // nothing was read then, and the panel stopped reading gloxid).
    global.stage.set_key_focus(null);
    findWindow(OTHER).activate(global.get_current_time());
    await sleep(300);
    const away = new MessageTray.Notification({source: gloxiSource, title: 'Нагадування', body: 'Обід о 13:00'});
    gloxiSource.addNotification(away);
    await sleep(300);
    check('side panel: focus in a window, a Gloxi notification is still read, and no banner',
        [panelFocused(), heard.at(-1)?.[0], away.acknowledged], [false, 'Нагадування: Обід о 13:00', true]);
    listAccessible.disconnect(hearing);

    settings.set_string('gloxi-dock-size', 'one-third');
    await sleep(800);
    check('strip: one third', dockPanel(), [1280, top, 640, mh - top]);
    settings.set_string('gloxi-dock-size', 'one-sixth');
    await sleep(500);

    view.focus();
    await sleep(200);
    await press(Clutter.KEY_Escape);
    await sleep(500);
    check('side panel: Escape closes it and gives the space back',
        [settings.get_boolean('gloxi-panel-open'), workArea()[2]], [false, mw]);
    check('side panel: no focus left in it', panelFocused(), false);
    const later = new MessageTray.Notification({source: gloxiSource, title: 'Gloxi', body: 'Готово'});
    gloxiSource.addNotification(later);
    await sleep(300);
    check('side panel: closed, a Gloxi notification is a banner again', later.acknowledged, false);
    gloxiSource.destroy();
    check('side panel: the strip is gone after sliding out', await waitFor(() => dockPanel() === null, 2000), true);

    // A window that is not maximised: the strut alone leaves it under the
    // panel (Build12.29), so the panel moves it out and back.
    const loose = findWindow(OTHER);
    if (Meta.MaximizeFlags)
        loose.unmaximize(Meta.MaximizeFlags.BOTH);
    else
        loose.unmaximize();
    await sleep(500);
    loose.move_resize_frame(true, 1300, 200, 500, 400);
    const placed = await waitFor(() => loose.get_frame_rect().x === 1300, 2000);
    const rightOf = r => r.x + r.width;
    settings.set_boolean('gloxi-panel-open', true);
    await sleep(800);
    const stripX = dockPanel()?.[0] ?? 0;
    check('strip: a window under it moves left of it, same width',
        placed && await waitFor(() => rightOf(loose.get_frame_rect()) <= stripX, 2000) &&
            loose.get_frame_rect().width, 500);
    settings.set_boolean('gloxi-panel-open', false);
    check('strip: closed, the window goes back',
        await waitFor(() => loose.get_frame_rect().x === 1300, 2000), true);
    settings.set_boolean('gloxi-panel-open', true);
    await sleep(800);
    global.display.emit('grab-op-begin', loose, Meta.GrabOp.MOVING);
    global.display.emit('grab-op-end', loose, Meta.GrabOp.MOVING);
    const kept = loose.get_frame_rect().x;
    settings.set_boolean('gloxi-panel-open', false);
    await sleep(800);
    check('strip: a window the person moved meanwhile stays where they left it',
        [kept < 1300, loose.get_frame_rect().x], [true, kept]);
    await waitFor(() => dockPanel() === null, 2000);

    await setShellProperty('GloxiPanelOpen', new GLib.Variant('b', true));
    await sleep(800);
    check('side panel: Gloxi opens it over D-Bus', Boolean(dockPanel()), true);
    // Wait for the state, not a set time: on a busy CI runner the virtual
    // pointer's press can arrive after the pause, and the next click then
    // looked like it had done nothing.
    await click(button);
    check('side panel: the top-bar button closes it',
        await waitFor(() => !settings.get_boolean('gloxi-panel-open'), 3000), true);
    await click(button);
    check('side panel: and opens it',
        await waitFor(() => settings.get_boolean('gloxi-panel-open'), 3000), true);
    await sleep(300);
    check('side panel: a new panel says hello again', panelRequests.filter(r => r.method === 'hello').length >= 2, true);

    // A second monitor: the panel opens where the focused window is, and
    // only that monitor gives up the strip, also on the edge between them.
    if (Main.layoutManager.monitors.length > 1) {
        settings.set_boolean('gloxi-panel-open', false);
        await sleep(800);
        const second = Main.layoutManager.monitors[1];
        const away = findWindow(OTHER);
        if (Meta.MaximizeFlags)
            away.unmaximize(Meta.MaximizeFlags.BOTH);
        else
            away.unmaximize();
        away.move_to_monitor(1);
        away.activate(global.get_current_time());
        await sleep(500);
        // What Super+G and the top-bar button call (both checked above). Not
        // the key itself: by now the layout is 'ua', which has no keysym g
        // for the virtual keyboard to send; a real keyboard sends the key.
        theDock().toggle();
        await sleep(800);
        const strip = dockPanel();
        const area = Main.layoutManager.getWorkAreaForMonitor(1);
        check('second monitor: the panel opens on the focused window\'s monitor',
            Boolean(strip) && [strip[0] > second.x, strip[0] + strip[2], strip[3]],
            [true, second.x + second.width, second.height]);
        check('second monitor: its work area stops at the strip, the primary keeps all of its width',
            [area.x + area.width, workArea()[2]], [strip?.[0], mw]);
        if (Meta.MaximizeFlags)
            away.maximize(Meta.MaximizeFlags.BOTH);
        else
            away.maximize();
        await sleep(800);
        const r = away.get_frame_rect();
        check('second monitor: a maximised window there stays left of the strip',
            [r.x >= second.x, r.x + r.width <= (strip?.[0] ?? 0)], [true, true]);
        check('second monitor: ready to type there too', panelFocused(), true);
        await press(Clutter.KEY_Escape);
        await sleep(600);
        check('second monitor: closed, the space comes back',
            [settings.get_boolean('gloxi-panel-open'), Main.layoutManager.getWorkAreaForMonitor(1).width],
            [false, second.width]);
        away.move_to_monitor(0);
        settings.set_boolean('gloxi-panel-open', true);
        await sleep(800);
    }

    settings.set_boolean('gloxi-dock', false);
    await sleep(800);
    check('side panel: off, no strip and no button',
        [dockPanel(), workArea()[2], Boolean(Main.panel.statusArea['vilna-gloxi-panel'])], [null, mw, false]);
    settings.reset('gloxi-panel-open');
}

const theWidgets = () => Main.extensionManager.lookup(UUID)?.stateObj?._userParts
    ?.find(p => p.constructor.name === 'Widgets') ?? null;
const theSky = () => theWidgets()?.sky ?? null;

// The live sky moves only when it can be seen and animations are on.
// windows() leaves the other window maximised, over the widgets.
async function skyMotion() {
    const softwareOnly = !St.Settings.get().enable_animations;
    if (softwareOnly)
        St.Settings.get().uninhibit_animations();
    const sky = theSky();
    const other = findWindow(OTHER);
    const dialog = findWindow(HERKOS);
    check('sky: still under a window', await waitFor(() => sky?.moving === false, 2500), true);
    dialog?.minimize();
    other.minimize();
    check('sky: moves once the window is gone', await waitFor(() => sky?.moving === true, 2500), true);
    check('sky: sized like the card', [sky._sky?.w, sky._sky?.h] , [Math.round(sky.actor.width), Math.round(sky.actor.height)]);
    check('sky: as wide as the column', sky._sky?.w, 340);
    Main.overview.show();
    check('sky: still in the overview', await waitFor(() => sky?.moving === false, 2500), true);
    Main.overview.hide();
    check('sky: moves after the overview', await waitFor(() => sky?.moving === true, 3000), true);
    St.Settings.get().inhibit_animations();
    check('sky: still with animations off', await waitFor(() => sky?.moving === false, 2500), true);
    if (!softwareOnly)
        St.Settings.get().uninhibit_animations();
    other.unminimize();
    dialog?.unminimize();
    check('sky: still under the window again', await waitFor(() => sky?.moving === false, 2500), true);
}

async function locked() {
    const entry = new St.PasswordEntry({width: 200});
    Main.layoutManager.addTopChrome(entry);
    await useLayout('ua');
    Main.sessionMode.pushMode('unlock-dialog');
    await sleep(800);
    check('lock: mode', Main.sessionMode.currentMode, 'unlock-dialog');
    check('lock: extension stays enabled', Main.extensionManager.lookup(UUID)?.state, ENABLED);
    await focus(entry);
    check('lock: password field Latin', current(), 'us');
    await focus(null);
    check('lock: layout given back', current(), 'ua');
    Main.sessionMode.popMode('unlock-dialog');
    await sleep(800);
    check('unlocked: extension enabled', Main.extensionManager.lookup(UUID)?.state, ENABLED);
    entry.destroy();
}

// The dock's Vilna colours (colorScheme.js, dockLook.js, lib/dock.js) and
// its springy icons (dockMotion.js). Dash to Dock is not installed here: a
// stand-in with its names and shape takes the styles and the motion.
function fakeDock(side) {
    const container = new St.Widget({name: 'dashtodockContainer', style_class: side, layout_manager: new Clutter.BinLayout()});
    const dash = new St.Widget({name: 'dash', layout_manager: new Clutter.BinLayout()});
    const background = new St.Widget({style_class: 'dash-background'});
    const dashContainer = new St.BoxLayout({name: 'dashtodockDashContainer'});
    const boxContainer = new St.BoxLayout({name: 'dashtodockBoxContainer'});
    const box = new St.BoxLayout();
    dash.add_child(background);
    dash.add_child(dashContainer);
    dashContainer.add_child(boxContainer);
    boxContainer.add_child(box);
    container.add_child(dash);
    const icons = [];
    for (let i = 0; i < 3; i++) {
        const button = new St.Button({track_hover: true, reactive: true});
        button.icon = new St.Widget({style_class: 'overview-icon', width: 48, height: 48});
        button.set_child(button.icon);
        const item = new St.Bin({child: button});
        box.add_child(item);
        icons.push(button);
    }
    Main.uiGroup.add_child(container);
    return {container, background, icons};
}

const dockParts = () => Main.extensionManager.lookup(UUID)?.stateObj?._userParts ?? [];
const theDockMotion = () => dockParts().find(p => p.constructor.name === 'DockMotion') ?? null;

async function dockColours(settings) {
    const iface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
    const classes = () => ['vilna-light', 'vilna-dark'].filter(c => Main.uiGroup.has_style_class_name(c));
    let {container, background} = fakeDock('bottom');
    const colour = () => background.get_theme_node().get_background_color();
    const rgb = () => [colour().red, colour().green, colour().blue];

    iface.set_string('color-scheme', 'default');
    await waitFor(() => classes().length === 1 && classes()[0] === 'vilna-light');
    check('dock: light style by default', classes(), ['vilna-light']);
    check('dock: cream glass in the light style', rgb(), [255, 252, 246]);
    const solid = colour().alpha;
    check('dock: the glass is nearly solid by default', solid > 200 && solid < 255, true);
    iface.set_string('color-scheme', 'prefer-dark');
    await waitFor(() => classes()[0] === 'vilna-dark');
    check('dock: dark style follows the setting', classes(), ['vilna-dark']);
    check('dock: night glass in the dark style', rgb(), [14, 26, 46]);

    // Transparency: more of it, less of the glass; 100 % leaves none.
    settings.set_int('dock-transparency', 50);
    check('dock: more transparent at 50 %', await waitFor(() => colour().alpha < solid && colour().alpha > 0), true);
    settings.set_int('dock-transparency', 100);
    check('dock: no glass at 100 %', await waitFor(() => colour().alpha === 0), true);
    settings.set_int('dock-transparency', 0);
    check('dock: solid at 0 %', await waitFor(() => colour().alpha === 255), true);
    settings.reset('dock-transparency');
    await waitFor(() => colour().alpha === solid);

    // Every side takes the glass, not only the bottom.
    container.destroy();
    const column = () => widgetsRoot()?.x ?? null;
    const alone = column();
    ({container, background} = fakeDock('left'));
    check('dock: night glass on the left too', rgb(), [14, 26, 46]);
    check('dock: widgets keep to the right of a dock on the left',
        await waitFor(() => column() !== null && column() >= alone + container.width), true);
    container.destroy();
    check('dock: widgets back at the edge without it', await waitFor(() => column() === alone), true);

    iface.set_string('color-scheme', 'default');
    await waitFor(() => classes()[0] === 'vilna-light');
    check('dock: back to light', classes(), ['vilna-light']);
}

async function dockIcons(settings) {
    // GNOME Shell turns animations off when it draws in software.
    const softwareOnly = !St.Settings.get().enable_animations;
    if (softwareOnly)
        St.Settings.get().uninhibit_animations();
    const {container, icons} = fakeDock('bottom');
    const motion = theDockMotion();
    const [a, b] = icons;
    check('icons: the motion follows the stand-in', await waitFor(() => motion?.stateOf(a) === 'still'), true);

    a.hover = true;
    check('icons: an icon grows under the pointer', await waitFor(() => a.icon.scale_x > 1.05, 2000), true);
    check('icons: it grows from the dock edge, upwards', [a.icon.pivot_point.x, a.icon.pivot_point.y], [0.5, 1]);
    check('icons: the others stay as they are', b.icon.scale_x, 1);
    a.hover = false;
    check('icons: and settles when the pointer leaves', await waitFor(() => a.icon.scale_x === 1, 2000), true);

    Main.overview.emit('item-drag-begin', a);
    check('icons: the others sway while one is dragged',
        await waitFor(() => motion.stateOf(b) === 'jiggle' && b.icon.rotation_angle_z !== 0, 2000), true);
    check('icons: the dragged one does not', motion.stateOf(a) !== 'jiggle' && a.icon.rotation_angle_z === 0, true);
    Main.overview.emit('item-drag-end');
    check('icons: still after the drag', await waitFor(() => b.icon.rotation_angle_z === 0, 2000), true);

    settings.set_string('dock-icon-motion', 'off');
    a.hover = true;
    await sleep(400);
    check('icons: off by the setting', a.icon.scale_x, 1);
    a.hover = false;
    settings.reset('dock-icon-motion');

    St.Settings.get().inhibit_animations();
    a.hover = true;
    await sleep(400);
    check('icons: none with animations off', a.icon.scale_x, 1);
    a.hover = false;
    St.Settings.get().uninhibit_animations();
    if (softwareOnly)
        St.Settings.get().inhibit_animations();
    container.destroy();
}

// The login screen draws its background on the actor named lockDialogGroup
// (screenShield.js). The container has no logind, so the Shell makes no
// screen shield here: style a stand-in with the same name.
// Every label text under an actor, in order.
function texts(actor) {
    const out = [];
    const walk = a => {
        if (a instanceof St.Label)
            out.push(a.text);
        for (const child of a.get_children())
            walk(child);
    };
    if (actor)
        walk(actor);
    return out;
}

const widgetsRoot = () => Main.layoutManager._backgroundGroup.get_children().find(a => a.name === 'vilnaWidgets') ?? null;
const widget = name => {
    const find = a => a.name === name ? a : a.get_children().map(find).find(Boolean) ?? null;
    const root = widgetsRoot();
    return root ? find(root) : null;
};

async function widgets() {
    const settings = vilnaSettings();
    const root = widgetsRoot();
    check('widgets: shown by default', Boolean(root), true);
    if (!root)
        return;
    // The wallpapers are a group at the bottom of the window group.
    const backgrounds = Main.layoutManager._backgroundGroup;
    check('widgets: above the wallpaper, under the windows',
        [backgrounds.get_last_child() === root, global.window_group.get_first_child() === backgrounds], [true, true]);
    check('widgets: left, below the top bar', [root.x, root.y], [24, Main.panel.height + 24]);
    const [time] = texts(widget('vilnaClock'));
    check('widgets: the clock shows the time', /^\d{1,2}:\d{2}$/.test(time ?? ''), true);
    const weatherText = () => texts(widget('vilnaWeatherWidget'));
    check('widgets: weather for the capital by default (Madrid)',
        await waitFor(() => weatherText()[2]?.startsWith('Madrid · '), 5000) ? weatherText().slice(1, 3) : weatherText(),
        ['18°', 'Madrid · Clear · ↑21° ↓11°']);
    const sky = theSky();
    check('sky: a clear day behind the weather, with dark text',
        [sky?.kind, sky?.actor.visible, widget('vilnaWeatherWidget')?.get_last_child().has_style_class_name('vilna-sky-ink-dark')],
        ['clear', true, true]);
    const alertTexts = () => texts(widget('vilnaWeatherAlerts'));
    check('warnings: the zone is found on this computer', await waitFor(() =>
        settings.get_value('alert-zones').deepUnpack()[2].join() === 'ES219', 15000)
        ? settings.get_value('alert-zones').deepUnpack()[2]
        : [settings.get_value('alert-zones').deepUnpack(), theWidgets()?.weatherAlerts?.status], ['ES219']);
    check('warnings: orange wind in the zone and yellow fog around it, the original words and the service',
        await waitFor(() => alertTexts().length === 6, 10000)
            ? alertTexts().map(s => s.replace(/(\b[A-Z][a-z]{2} )?\d{1,2}:\d{2}/g, 'T'))
            : alertTexts(),
        ['Orange warning: Wind · until T', 'Orange test warning. Test zone', 'Test Service · issued T',
            'Yellow warning: Fog · until T', 'Yellow Fog Warning issued for Spain - Área de prueba',
            'EUMETNET – MeteoAlarm · issued T']);
    check('warnings: words already in the person\u2019s language, Gloxi not asked', gloxiRequests.length, 0);
    // The first refresh came before passwordFields() turned the extension
    // off and on, which took its notification away; the warning was
    // remembered, so it did not come again.
    const stateFile = GLib.build_filenamev([GLib.get_user_state_dir(), 'vilna', 'alerts-notified.json']);
    const notified = () => {
        try {
            return new TextDecoder().decode(GLib.file_get_contents(stateFile)[1]);
        } catch {
            return '';
        }
    };
    const warningNotes = () => Main.messageTray.getSources().flatMap(s => s.notifications ?? [])
        .filter(n => /warning:/.test(n.title)).map(n => [n.title, n.urgency]);
    check('warnings: remembered, so it comes once', [notified(), warningNotes()], ['["test.orange.wind"]', []]);
    // Forget it and start the widgets again: the orange one comes, the
    // yellow one does not.
    GLib.unlink(stateFile);
    settings.set_boolean('widgets', false);
    settings.set_boolean('widgets', true);
    check('warnings: one notification, for the orange one only',
        await waitFor(() => warningNotes().length > 0, 10000) ? warningNotes() : [warningNotes(), notified()],
        [['Orange warning: Wind', 2]]);
    // A person reading Ukrainian: the service gave no Ukrainian words, so
    // Gloxi translates them and the translation stands above the original.
    const alerts = theWidgets()?.weatherAlerts;
    alerts._languages = ['uk_UA.UTF-8', 'uk'];
    alerts._caps.clear();
    alerts.update();
    check('warnings: Gloxi\u2019s translation above the unchanged original',
        await waitFor(() => alertTexts().length === 8, 10000)
            ? alertTexts().map(s => s.replace(/(\b[A-Z][a-z]{2} )?\d{1,2}:\d{2}/g, 'T'))
            : [alertTexts(), alerts.status],
        ['Orange warning: Wind · until T', '[uk] Orange test warning. Test zone', 'Orange test warning. Test zone',
            'Test Service · issued T · translated by Gloxi',
            'Yellow warning: Fog · until T', '[uk] Yellow Fog Warning issued for Spain - Área de prueba',
            'Yellow Fog Warning issued for Spain - Área de prueba',
            'EUMETNET – MeteoAlarm · issued T · translated by Gloxi']);
    check('warnings: Gloxi was asked for Ukrainian, once per text',
        gloxiRequests.map(r => [r.method, r.params.language]), [['text.translate', 'uk'], ['text.translate', 'uk']]);
    alerts.update();
    await sleep(1000);
    check('warnings: a translation is kept, not asked for again', gloxiRequests.length, 2);
    settings.set_boolean('weather-alerts', false);
    check('warnings: off by the setting', await waitFor(() => !widget('vilnaWeatherAlerts')?.visible, 3000), true);
    settings.set_boolean('weather-alerts', true);
    check('sky: the icon gives way to the sky', widget('vilnaWeatherWidget')?.get_last_child()
        .get_children().flatMap(c => c.get_children?.() ?? []).some(c => c instanceof St.Icon && c.visible), false);
    settings.set_value('weather-location', new GLib.Variant('(sdd)', ['València', 39.47, -0.38]));
    check('widgets: a chosen place wins', await waitFor(() => weatherText()[2]?.startsWith('València · '), 5000), true);
    settings.set_value('weather-location', new GLib.Variant('(sdd)', ['', 0, 0]));
    await waitFor(() => weatherText()[2]?.startsWith('Madrid · '), 5000);
    settings.set_boolean('weather', false);
    await sleep(200);
    check('widgets: weather can be hidden', widget('vilnaWeatherWidget')?.visible, false);
    settings.set_boolean('weather', true);

    check('widgets: Копії not set up yet', texts(widget('vilnaBackupWidget'))[0], 'Backups are not set up');
    const dir = GLib.build_filenamev([GLib.get_user_state_dir(), 'vilna-backup']);
    GLib.mkdir_with_parents(dir, 0o700);
    const now = Math.floor(Date.now() / 1000);
    GLib.file_set_contents(`${dir}/runs.jsonl`, `${JSON.stringify({time: now, kind: 'backup', ok: true})}\n`);
    check('widgets: Копії saved today', await waitFor(() => texts(widget('vilnaBackupWidget'))[0] === 'Backups: all saved', 5000)
        ? texts(widget('vilnaBackupWidget'))[1]?.startsWith('Last: today at ') : texts(widget('vilnaBackupWidget')), true);
    check('widgets: calendar card says nothing is planned', texts(widget('vilnaCalendarWidget')).slice(1), ['Nothing planned']);
    check('widgets: Gloxi card', texts(widget('vilnaGloxiWidget')).slice(0, 2), ['GLOXI', 'How can I help?']);

    Main.overview.show();
    await waitFor(() => Main.overview.visible && !Main.overview.animationInProgress);
    check('widgets: hidden in the overview', widgetsRoot()?.visible, false);
    Main.overview.hide();
    await waitFor(() => !Main.overview.visible && !Main.overview.animationInProgress);
    check('widgets: back after the overview', widgetsRoot()?.visible, true);

    settings.set_boolean('widgets', false);
    await sleep(200);
    check('widgets: off by the setting', widgetsRoot(), null);
    settings.set_boolean('widgets', true);
    await sleep(200);
    check('widgets: on again', Boolean(widgetsRoot()), true);
}

async function loginBackground() {
    const group = new St.Widget({name: 'lockDialogGroup'});
    Main.layoutManager.uiGroup.add_child(group);
    await sleep(100);
    const node = group.get_theme_node();
    check('login screen: Vilna background',
        node.get_background_image()?.get_path() ?? null,
        '/usr/share/backgrounds/vilna/vilna-login.jpg');
    check('login screen: night colour beneath it', node.get_background_color().to_string(), '#0e1a2eff');
    group.destroy();
}

async function run() {
    spyEases();
    const ok = await waitFor(() => Main.extensionManager.lookup(UUID)?.state === ENABLED, 10000);
    check('extension enabled', ok, true);
    check('no extension error', Main.extensionManager.lookup(UUID)?.error ?? '', '');
    if (!ok)
        return;
    const mode = Main.sessionMode.currentMode;
    // A session starts in the overview with its search entry focused; when
    // the overview closes, focus goes back to the stage. Let that settle
    // first, or it takes focus from the first test field.
    if (Main.overview.visible)
        Main.overview.hide();
    await waitFor(() => !Main.overview.visible && !Main.overview.animationInProgress);
    await sleep(500);
    if (mode !== 'gdm') {
        const inputSources = new Gio.Settings({schema_id: 'org.gnome.desktop.input-sources'});
        inputSources.set_value('sources', new GLib.Variant('a(ss)', [['xkb', 'ua'], ['xkb', 'us'], ['xkb', 'pl']]));
    }
    // On the login screen the layouts come from localed (run.sh fakes it).
    check('three layouts', await waitFor(() => ['ua', 'us', 'pl'].every(source)), true);

    await passwordFields();
    if (mode === 'gdm') {
        check('login screen: no D-Bus service', await nameOwned('io.github.stdenis1980.VilnaShell'), false);
        check('login screen: no dock style class',
            ['vilna-light', 'vilna-dark'].some(c => Main.uiGroup.has_style_class_name(c)), false);
        check('login screen: no widgets', widgetsRoot(), null);
        await loginBackground();
        return;
    }
    await dockColours(vilnaSettings());
    await dockIcons(vilnaSettings());
    await widgets();
    await locked();
    await windows();
    await skyMotion();
}

// A stand-in for gloxid (gloxi/docs/service-protocol.md) on
// GLOXI_SERVICE_SOCKET. text.translate answers "[uk] " + the text and keeps
// the requests, so the checks see what the weather card asked. The side
// panel's hello gets a short history, and chat.submit an answer streamed
// as events, as gloxid sends them.
const gloxiRequests = [];
const panelRequests = [];
const PANEL_HISTORY = [{role: 'user', text: 'Що сьогодні?'}, {role: 'tool', text: 'never shown'},
    {role: 'assistant', text: 'О 15:00 зустріч з Дмитром.'}];
function fakeAnswer(request) {
    if (request.method === 'text.translate') {
        gloxiRequests.push(request);
        // An event first, as a busy gloxid may send: it is not the answer.
        return [{event: 'busy', busy: false},
            {id: request.id, result: {text: `[${request.params.language}] ${request.params.text}`}}];
    }
    panelRequests.push(request);
    if (request.method === 'hello')
        return [{id: request.id, result: {protocol: 2, version: 'test', state: {busy: false}, history: PANEL_HISTORY}}];
    if (request.method === 'chat.submit') {
        return [{id: request.id, result: {accepted: true}}, {event: 'busy', busy: true},
            {event: 'user_message', text: request.params.text}, {event: 'start_answer', label: 'On this computer'},
            {event: 'append_answer', piece: 'Готово: '}, {event: 'append_answer', piece: 'темну тему ввімкнено.'},
            {event: 'action', card: {status: 'done', program: 'Settings', operation: 'dark theme'}},
            {event: 'finish_answer'}, {event: 'busy', busy: false}];
    }
    return [{id: request.id, error: {code: 'unknown_method', message: request.method}}];
}

function fakeGloxid() {
    const path = GLib.getenv('GLOXI_SERVICE_SOCKET');
    if (!path)
        return;
    const service = new Gio.SocketService();
    service.add_address(Gio.UnixSocketAddress.new(path), Gio.SocketType.STREAM, Gio.SocketProtocol.DEFAULT, null);
    service.connect('incoming', (_s, conn) => {
        const input = new Gio.DataInputStream({base_stream: conn.get_input_stream()});
        const next = () => input.read_line_async(GLib.PRIORITY_DEFAULT, null, (_i, result) => {
            try {
                const [line] = input.read_line_finish_utf8(result);
                if (line === null)
                    return;
                const lines = fakeAnswer(JSON.parse(line));
                conn.get_output_stream().write_all(new TextEncoder().encode(
                    lines.map(l => `${JSON.stringify(l)}\n`).join('')), null);
                next();
            } catch (e) {
                console.warn(`vilna fake gloxid: ${e.message}`);
            }
        });
        next();
        return true;
    });
    service.start();
    globalThis.__vilnaFakeGloxid = service;
}

export default class VilnaTest extends Extension {
    enable() {
        // Enabled again whenever the Vilna extension is; run only once.
        if (globalThis.__vilnaTestRan)
            return;
        globalThis.__vilnaTestRan = true;
        fakeGloxid();
        run().catch(e => check('no exception', `${e}\n${e.stack}`, null)).finally(flush);
    }

    disable() {
    }
}

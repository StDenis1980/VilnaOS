// SPDX-License-Identifier: GPL-3.0-or-later
//
// Test helper for tests/shell/run.sh: runs inside a headless GNOME Shell
// next to the Vilna extension, drives it, and writes PASS/FAIL lines to
// $VILNA_TEST_OUT. Never installed outside the test.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
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
    settings.set_boolean('gloxi-dock', true);
    await sleep(800);
    check('strip: work area without it', workArea(), [mx, top, 1600, mh - top]);
    check('strip: panel below the top bar', dockPanel(), [1600, top, 320, mh - top]);
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
    settings.set_string('gloxi-dock-size', 'one-third');
    await sleep(800);
    check('strip: one third', dockPanel(), [1280, top, 640, mh - top]);
    findWindow(GLOXI).minimize();
    await sleep(800);
    check('motion: minimise in 450 ms', lastEase(GLOXI), [450, Mode.EASE_IN_OUT_CUBIC]);
    check('strip: released while Gloxi is minimised', dockPanel(), null);
    findWindow(GLOXI).unminimize();
    await sleep(800);
    check('motion: unminimise in 450 ms', lastEase(GLOXI), [450, Mode.EASE_IN_OUT_CUBIC]);
    check('motion: shown window settled', [findWindow(GLOXI).get_compositor_private()?.opacity,
        findWindow(GLOXI).get_compositor_private()?.scale_x], [255, 1]);
    check('strip: back when Gloxi is shown', Boolean(dockPanel()), true);
    findWindow(GLOXI).delete(global.get_current_time());
    await sleep(1500);
    check('motion: close in 196 ms', lastEase(GLOXI), [196, Mode.EASE_IN_CUBIC]);
    check('strip: released when Gloxi closes', [dockPanel(), workArea()[2]], [null, mw]);
    settings.set_boolean('gloxi-dock', false);
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

// The dock's Vilna colours (colorScheme.js and stylesheet.css). Dash to
// Dock is not installed here: a stand-in with its names takes the styles.
async function dockColours() {
    const iface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
    const classes = () => ['vilna-light', 'vilna-dark'].filter(c => Main.uiGroup.has_style_class_name(c));
    const container = new St.Widget({name: 'dashtodockContainer', style_class: 'bottom'});
    const dash = new St.Widget({name: 'dash'});
    const background = new St.Widget({style_class: 'dash-background'});
    dash.add_child(background);
    container.add_child(dash);
    Main.uiGroup.add_child(container);
    const colour = () => background.get_theme_node().get_background_color().to_string();

    iface.set_string('color-scheme', 'default');
    await waitFor(() => classes().length === 1 && classes()[0] === 'vilna-light');
    check('dock: light style by default', classes(), ['vilna-light']);
    check('dock: cream glass in the light style', colour(), '#fffcf6dc');
    iface.set_string('color-scheme', 'prefer-dark');
    await waitFor(() => classes()[0] === 'vilna-dark');
    check('dock: dark style follows the setting', classes(), ['vilna-dark']);
    check('dock: night glass in the dark style', colour(), '#0e1a2ed1');
    iface.set_string('color-scheme', 'default');
    await waitFor(() => classes()[0] === 'vilna-light');
    check('dock: back to light', classes(), ['vilna-light']);
    container.destroy();
}

// The login screen draws its background on the actor named lockDialogGroup
// (screenShield.js). The container has no logind, so the Shell makes no
// screen shield here: style a stand-in with the same name.
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
        await loginBackground();
        return;
    }
    await dockColours();
    await locked();
    await windows();
}

export default class VilnaTest extends Extension {
    enable() {
        // Enabled again whenever the Vilna extension is; run only once.
        if (globalThis.__vilnaTestRan)
            return;
        globalThis.__vilnaTestRan = true;
        run().catch(e => check('no exception', `${e}\n${e.stack}`, null)).finally(flush);
    }

    disable() {
    }
}

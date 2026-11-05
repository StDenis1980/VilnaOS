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
    check('a Latin layout (Polish) is kept', current(), 'pl');
    await focus(null);

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

async function windows() {
    const settings = vilnaSettings();
    GLib.file_set_contents(`${GLib.getenv('VILNA_TEST_OUT')}.windows`, '');
    const ready = await waitFor(() => [GLOXI, HERKOS, OTHER].every(findWindow), 15000);
    check('test windows appeared', ready, true);
    if (!ready)
        return;
    await sleep(500);

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

    check('D-Bus name owned in the user session', await nameOwned('io.github.stdenis1980.VilnaShell'), true);

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
    check('strip: released while Gloxi is minimised', dockPanel(), null);
    findWindow(GLOXI).unminimize();
    await sleep(800);
    check('strip: back when Gloxi is shown', Boolean(dockPanel()), true);
    findWindow(GLOXI).delete(global.get_current_time());
    await sleep(1500);
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

async function run() {
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
        return;
    }
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

// SPDX-License-Identifier: GPL-3.0-or-later
//
// A stand-in window for tests/shell/run.sh: `gjs -m window.js APP_ID KIND`
// opens one GTK 4 window under that application id, holding a single
// focused text field: a password entry (KIND=password) or a plain one.

import Gtk from 'gi://Gtk?version=4.0';

const [appId, kind] = ARGV;
const app = new Gtk.Application({application_id: appId});
app.connect('activate', () => {
    const window = new Gtk.ApplicationWindow({application: app, title: appId, default_width: 400, default_height: 300});
    window.set_child(kind === 'password' ? new Gtk.PasswordEntry() : new Gtk.Entry());
    window.present();
});
app.run([]);

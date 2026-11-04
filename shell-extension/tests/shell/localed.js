// SPDX-License-Identifier: GPL-3.0-or-later
//
// A stand-in for systemd-localed on the system bus, for the login-screen
// run of tests/shell/run.sh: the login screen takes its layouts from it.
// Owning the name needs root (or a bus policy that allows it).

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const XML = `<node><interface name="org.freedesktop.locale1">
  <property name="Locale" type="as" access="read"/>
  <property name="X11Layout" type="s" access="read"/>
  <property name="X11Model" type="s" access="read"/>
  <property name="X11Variant" type="s" access="read"/>
  <property name="X11Options" type="s" access="read"/>
  <property name="VConsoleKeymap" type="s" access="read"/>
</interface></node>`;

const exported = Gio.DBusExportedObject.wrapJSObject(XML, {
    Locale: ['LANG=uk_UA.UTF-8'],
    X11Layout: 'ua,us,pl',
    X11Model: '',
    X11Variant: ',,',
    X11Options: '',
    VConsoleKeymap: 'us',
});
exported.export(Gio.DBus.system, '/org/freedesktop/locale1');
Gio.bus_own_name_on_connection(Gio.DBus.system, 'org.freedesktop.locale1',
    Gio.BusNameOwnerFlags.REPLACE, () => print('owned'), () => print('lost'));
new GLib.MainLoop(null, false).run();

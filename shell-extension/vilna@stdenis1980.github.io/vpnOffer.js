// SPDX-License-Identifier: GPL-3.0-or-later
//
// (e) A VPN offer after the login page of a shared Wi-Fi (ADR 0027,
// section 6).
//
// NetworkManager says when a network asks for a login and when the internet
// answers. After such a login, with no tunnel up, the Shell shows one
// notification for that network: "Set up a VPN" opens Gloxi with its VPN
// talk (the desktop action `vpn`, gloxi-gtk --vpn). The Shell only offers:
// connecting and the provider's login stay in the provider's app. The
// networks already offered are kept in the setting vpn-offered-networks.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import NM from 'gi://NM';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';

import {WifiOffer, offerText} from './lib/wifiOffer.js';

const GLOXI = 'io.github.stdenis1980.Gloxi.desktop';
// The tunnel interfaces vilna-netguard knows (ruleset TUNNELS).
const TUNNEL = /^(wg|proton|pvpn|tun)/;

function connectivityName(state) {
    switch (state) {
    case NM.ConnectivityState.FULL: return 'full';
    case NM.ConnectivityState.PORTAL: return 'portal';
    case NM.ConnectivityState.LIMITED: return 'limited';
    case NM.ConnectivityState.NONE: return 'none';
    default: return 'unknown';
    }
}

export class VpnOffer {
    constructor(settings) {
        this._settings = settings;
        this._client = null;
        this._signals = [];
        this._cancellable = null;
        this._offer = null;
    }

    enable() {
        this._offer = new WifiOffer(this._settings.get_strv('vpn-offered-networks'));
        this._cancellable = new Gio.Cancellable();
        NM.Client.new_async(this._cancellable, (_o, res) => {
            try {
                this._client = NM.Client.new_finish(res);
            } catch (e) {
                if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    console.warn(`Vilna: no NetworkManager for the VPN offer: ${e.message}`);
                return;
            }
            for (const signal of ['notify::connectivity', 'notify::primary-connection'])
                this._signals.push(this._client.connect(signal, () => this._check()));
            this._check();
        });
    }

    disable() {
        this._cancellable?.cancel();
        this._cancellable = null;
        for (const id of this._signals)
            this._client?.disconnect(id);
        this._signals = [];
        this._client = null;
        this._offer = null;
    }

    _vpnUp() {
        const active = this._client.get_active_connections() ?? [];
        if (active.some(c => c.get_vpn?.() || c.get_connection_type() === 'wireguard'))
            return true;
        // Mullvad's tunnel is not a NetworkManager connection, only a device.
        return (this._client.get_devices() ?? []).some(d => TUNNEL.test(d.get_iface() ?? ''));
    }

    _check() {
        if (!this._client || !this._offer)
            return;
        const primary = this._client.get_primary_connection();
        const show = this._offer.update({
            connectivity: connectivityName(this._client.get_connectivity()),
            uuid: primary?.get_uuid() ?? null,
            wifi: primary?.get_connection_type() === '802-11-wireless',
            vpnUp: this._vpnUp(),
        });
        if (!show)
            return;
        this._settings.set_strv('vpn-offered-networks', this._offer.offered);
        this._notify();
    }

    _notify() {
        const text = offerText(GLib.get_language_names());
        const source = MessageTray.getSystemSource();
        const notification = new MessageTray.Notification({
            source,
            title: text.title,
            body: text.body,
            iconName: 'network-vpn-symbolic',
        });
        notification.addAction(text.action, () => {
            const app = Gio.DesktopAppInfo.new(GLOXI);
            try {
                app?.launch_action('vpn', global.create_app_launch_context(0, -1));
            } catch (e) {
                console.warn(`Vilna: could not open Gloxi: ${e.message}`);
            }
        });
        source.addNotification(notification);
    }
}

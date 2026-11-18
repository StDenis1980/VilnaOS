// SPDX-License-Identifier: GPL-3.0-or-later
//
// Which MeteoAlarm warning zones a place lies in, run as its own gjs
// process by widgets.js so the 30 MB boundaries file never sits in
// GNOME Shell's memory:
//
//   gjs -m alertZones.js LAT LON [FILE]
//
// Prints a JSON list of zone codes (EMMA_ID), e.g. ["ES247"], and exits
// 0; on failure prints the reason and exits 1. The place stays here: the
// whole file is downloaded from MeteoAlarm's repository on GitLab and
// the match is made on this computer. The file is EUMETNET's; only the
// codes are kept (lib/alerts.js zonesAt). FILE, for the tests, is read
// instead of the download.

import GLib from 'gi://GLib';
import Soup from 'gi://Soup?version=3.0';
import System from 'system';

import {GEOCODES_TREE, latestGeocodes, zonesAt} from './lib/alerts.js';

// The file is about 30 MB; refuse anything far larger.
const MAX_BYTES = 200 * 1024 * 1024;

function fetch(session, url) {
    const message = Soup.Message.new('GET', url);
    const bytes = session.send_and_read(message, null);
    if (message.get_status() !== Soup.Status.OK)
        throw new Error(`HTTP ${message.get_status()} for ${url}`);
    if (bytes.get_size() > MAX_BYTES)
        throw new Error('the boundaries file is too large');
    return new TextDecoder().decode(bytes.get_data());
}

function main(argv) {
    const [lat, lon] = [Number(argv[0]), Number(argv[1])];
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
        print('usage: gjs -m alertZones.js LAT LON [FILE]');
        return 1;
    }
    let text;
    if (argv[2]) {
        const [ok, bytes] = GLib.file_get_contents(argv[2]);
        if (!ok)
            throw new Error(`cannot read ${argv[2]}`);
        text = new TextDecoder().decode(bytes);
    } else {
        const session = new Soup.Session({user_agent: 'Vilna desktop widgets', timeout: 120});
        const latest = latestGeocodes(fetch(session, GEOCODES_TREE));
        if (!latest)
            throw new Error('no boundaries file in the MeteoAlarm repository');
        text = fetch(session, latest.url);
    }
    print(JSON.stringify(zonesAt(JSON.parse(text), lat, lon)));
    return 0;
}

try {
    System.exit(main(ARGV));
} catch (e) {
    print(`error: ${e.message}`);
    System.exit(1);
}

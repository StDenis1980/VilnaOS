// SPDX-License-Identifier: GPL-3.0-or-later
//
// verticalBox(params): a vertical St.BoxLayout on Shell 46 to 51
// (lib/boxes.js says why).

import Clutter from 'gi://Clutter';
import St from 'gi://St';

import {verticalBoxParams} from './lib/boxes.js';

let hasOrientation = null;

function shellHasOrientation() {
    if (hasOrientation === null) {
        try {
            new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL}).destroy();
            hasOrientation = true;
        } catch {
            hasOrientation = false;
        }
    }
    return hasOrientation;
}

export function verticalBox(params = {}) {
    return new St.BoxLayout(verticalBoxParams(params, shellHasOrientation(), Clutter.Orientation.VERTICAL));
}

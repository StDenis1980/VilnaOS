#
# spec file for package vilna-presets
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

Name:           vilna-presets
Version:        0.1
Release:        0
Summary:        Vilna defaults for which systemd services are enabled
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        50-vilna.preset
BuildRequires:  systemd-rpm-macros
BuildArch:      noarch
# Adds Vilna's changes on top of the Aeon presets. Becomes
# systemd-presets-branding-Vilna (replacing systemd-presets-branding-Aeon)
# once the Aeon preset file is copied here, see obs/README.md.

%description
Service presets for Vilna: avahi, samba and openssh stay installed but
are not enabled by default; the Herkos and Gloxi services are enabled.

%prep

%build

%install
install -D -m 0644 %{SOURCE0} %{buildroot}%{_presetdir}/50-vilna.preset

%files
%{_presetdir}/50-vilna.preset

%changelog

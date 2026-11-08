#
# spec file for package plymouth-branding-Vilna
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

# Version comes from image/plymouth/VERSION; obs/tools/obs_sync.py fills it in.
Name:           plymouth-branding-Vilna
Version:        0.1
Release:        0
Summary:        Vilna boot splash
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        %{name}-%{version}.tar.gz
BuildRequires:  python3
BuildRequires:  rsvg-convert
Requires:       plymouth
Requires:       plymouth-dracut
Requires:       plymouth-plugin-label
Requires:       plymouth-plugin-script
Provides:       plymouth-branding
Conflicts:      otherproviders(plymouth-branding)
BuildArch:      noarch

%description
The Vilna boot splash, a Plymouth theme named "vilna": on a night-blue
screen the brush stroke of the Vilna logo closes into a circle, the
swallow flies out of it and Petrykivka-style stems grow in two corners;
three gold nodes pulse while the system starts. It asks for the disk
password in the same style and stays still when the login screen takes
over. It also carries plymouthd.defaults (theme vilna, no show delay,
8 s device timeout), as every plymouth-branding package does.

%prep
%autosetup -p1

%build
python3 make_theme.py Vilna_logo_dark-bg.svg build

%install
dest=%{buildroot}%{_datadir}/plymouth/themes/vilna
install -d "$dest"
install -m 0644 vilna.plymouth vilna.script build/*.png "$dest/"
# Distribution defaults of plymouthd, also copied into the initrd by
# plymouth-populate-initrd. The plymouth-branding package owns them.
install -D -m 0644 plymouthd.defaults %{buildroot}%{_datadir}/plymouth/plymouthd.defaults

%files
%license LICENSE
%doc README.md
%dir %{_datadir}/plymouth
%dir %{_datadir}/plymouth/themes
%{_datadir}/plymouth/themes/vilna/
%{_datadir}/plymouth/plymouthd.defaults

%changelog

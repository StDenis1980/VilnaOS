#
# spec file for package vilna-icon-theme
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

# Version comes from docs/design/icons/system/VERSION; obs/tools/obs_sync.py fills it in.
Name:           vilna-icon-theme
Version:        0.1
Release:        0
Summary:        Vilna icon themes: folders, trash, drives and Vilna symbolic icons
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        %{name}-%{version}.tar.gz
BuildRequires:  python3
Requires:       adwaita-icon-theme
Requires:       hicolor-icon-theme
BuildArch:      noarch

%description
The Vilna icon theme for GNOME. It draws only what is about Vilna and
seen most: the folders (home, documents, downloads, music, pictures,
videos, desktop, templates, public), the trash and the drives in the
colours of the Vilna logo, and the symbolic icons of Gloxi and Herkos
(masked data, local or cloud model, consent, key, model, microphone,
risk levels R0-R4). Everything else comes from Adwaita (Inherits).
Vilna Classic, a second theme to choose, draws the same folders, trash
and drives as real objects with depth, highlights and soft shadows, and
takes the symbolic icons from Vilna. The icons are drawn in code by
make_icons.py and make_classic.py, with nothing taken from other icon
themes.

%prep
%autosetup -p1

%build

%check
# The committed icons are what the generator draws, index.theme lists
# every folder, symbolic icons have one colour (GNOME recolours them).
python3 src/check_icons.py Vilna Vilna-Classic

%install
install -d %{buildroot}%{_datadir}/icons
for theme in Vilna Vilna-Classic; do
  cp -r "$theme" %{buildroot}%{_datadir}/icons/"$theme"
  find %{buildroot}%{_datadir}/icons/"$theme" -type d -exec chmod 0755 {} +
  find %{buildroot}%{_datadir}/icons/"$theme" -type f -exec chmod 0644 {} +
done

%files
%license LICENSE
%doc README.md
%{_datadir}/icons/Vilna/
%{_datadir}/icons/Vilna-Classic/

%changelog

#
# spec file for package vilna-backgrounds
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

Name:           vilna-backgrounds
Version:        0.1
Release:        0
Summary:        Vilna desktop wallpapers
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        vilna-backgrounds.xml
Source1:        vilna-sonce-light.jpg
Source2:        vilna-sonce-dark.jpg
Source3:        vilna-skhid-light.jpg
Source4:        vilna-skhid-dark.jpg
Source5:        vilna-tulpan-light.jpg
Source6:        vilna-tulpan-dark.jpg
Source7:        vilna-sonyashnyk-light.jpg
Source8:        vilna-sonyashnyk-dark.jpg
Source9:        vilna-voloshka-light.jpg
Source10:        vilna-voloshka-dark.jpg
Source11:        vilna-malva-light.jpg
Source12:        vilna-malva-dark.jpg
Source13:        vilna-blavet-light.jpg
Source14:        vilna-blavet-dark.jpg
BuildArch:      noarch

%description
The Vilna wallpapers for GNOME: one Petrykivka element on a play of light
and shadow, each as a light and a dark picture that GNOME switches with
the style. Сонце, Схід, Тюльпан, Соняшник, Волошка, Мальва and Blavet
(the wing of the kingfisher, for the first Vilna release). Сонце is the
default (gnome-branding-Vilna).

%prep

%build

%install
install -d %{buildroot}%{_datadir}/backgrounds/vilna
install -m 0644 %{SOURCE1} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE2} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE3} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE4} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE5} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE6} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE7} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE8} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE9} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE10} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE11} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE12} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE13} %{buildroot}%{_datadir}/backgrounds/vilna/
install -m 0644 %{SOURCE14} %{buildroot}%{_datadir}/backgrounds/vilna/
install -D -m 0644 %{SOURCE0} %{buildroot}%{_datadir}/gnome-background-properties/vilna-backgrounds.xml

%files
%dir %{_datadir}/backgrounds
%{_datadir}/backgrounds/vilna
%dir %{_datadir}/gnome-background-properties
%{_datadir}/gnome-background-properties/vilna-backgrounds.xml

%changelog

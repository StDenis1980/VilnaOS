#
# spec file for package vilna-software-picks
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

Name:           vilna-software-picks
Version:        0.2
Release:        0
Summary:        «Recommended by Vilna» apps in GNOME Software
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        picks.toml
Source1:        build_picks.py
BuildRequires:  python3 >= 3.11
BuildRequires:  libxml2-tools
BuildArch:      noarch
# Only GNOME Software reads these files; without it they do nothing.
Enhances:       gnome-software

%description
AppStream metadata that marks a curated list of free Flathub apps as
«Recommended by Vilna». GNOME Software shows them in their own section on
the Explore page and highlights them on the category pages. Gloxi and the
Herkos confirmation dialog read the same list (Vilna::Pick key) to know
whether an app is one of the picks.

The list and its rules are in picks.toml (ADR 0012 in the Vilna
repository). It is advice, not a restriction: every other Flathub app
stays available as usual.

%prep
cp %{SOURCE0} %{SOURCE1} .

%build
python3 build_picks.py --picks picks.toml build out

%install
install -D -m 0644 out/vilna-software-picks.xml %{buildroot}%{_datadir}/swcatalog/xml/vilna-software-picks.xml
install -D -m 0644 out/deployment-featured.ini %{buildroot}%{_datadir}/gnome-software/deployment-featured.ini

%check
python3 build_picks.py --picks picks.toml check
xmllint --noout %{buildroot}%{_datadir}/swcatalog/xml/vilna-software-picks.xml

%files
%dir %{_datadir}/swcatalog
%dir %{_datadir}/swcatalog/xml
%{_datadir}/swcatalog/xml/vilna-software-picks.xml
%dir %{_datadir}/gnome-software
%{_datadir}/gnome-software/deployment-featured.ini

%changelog

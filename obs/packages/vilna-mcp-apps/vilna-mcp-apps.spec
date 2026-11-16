#
# spec file for package vilna-mcp-apps
#
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Vilna contributors
#

# A tool program, not a library: built for the primary Python only.
%define pythons python3
# Version comes from mcp/apps/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           vilna-mcp-apps
Version:        0.1.0~dev0
Release:        0
Summary:        Herkos's tool for finding, installing and removing Flatpak apps
License:        Apache-2.0
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        %{name}-%{version}.tar.gz
BuildRequires:  %{python_module base >= 3.11}
BuildRequires:  %{python_module hatchling >= 1.26}
BuildRequires:  %{python_module pip}
BuildRequires:  %{python_module pytest >= 8}
BuildRequires:  %{python_module wheel}
BuildRequires:  fdupes
BuildRequires:  python-rpm-macros
BuildRequires:  sysuser-tools
Requires:       python3 >= 3.11
# libflatpak through PyGObject, the same library GNOME Software uses.
Requires:       flatpak
Requires:       python3-gobject
Requires:       typelib-1_0-Flatpak-1_0
# The launcher asks the person's systemd user manager to open the app.
Requires:       systemd
# «Рекомендовано Vilna»: search puts these apps first.
Recommends:     vilna-software-picks
%sysusers_requires
BuildArch:      noarch

%description
The MCP server "programs" of Vilna OS (ADR 0012) finds apps on Flathub and
in Vilna's own Flatpak repository, describes them (developer, verification,
licence, size, Flatpak permissions) and installs, updates and removes them
through libflatpak. Apps from «Рекомендовано Vilna» come first. It cannot
add a source of apps or install from a file. Only Herkos starts it
(vilna-mcp-apps@.service in the herkos package), as the system user
vilna-apps, whom polkit lets install, update and remove apps from Flathub
and Vilna only. The second server, vilna-mcp-launcher, opens an installed
app for the person.

%prep
%autosetup -p1

%build
%pyproject_wheel
%sysusers_generate_pre data/vilna-apps.sysusers vilna-apps vilna-apps.conf

%install
%pyproject_install
%python_expand %fdupes %{buildroot}%{$python_sitelib}
install -Dm0644 data/vilna-apps.sysusers %{buildroot}%{_sysusersdir}/vilna-apps.conf
install -Dm0644 data/50-vilna-apps.rules %{buildroot}%{_datadir}/polkit-1/rules.d/50-vilna-apps.rules

%check
%pytest

%pre -f vilna-apps.pre

%files
%license LICENSE
%doc README.md
%{_bindir}/vilna-mcp-apps
%{_bindir}/vilna-mcp-launcher
%{python_sitelib}/vilna_mcp_apps
%{python_sitelib}/vilna_mcp_apps-*.dist-info
%{_sysusersdir}/vilna-apps.conf
%{_datadir}/polkit-1/rules.d/50-vilna-apps.rules

%changelog

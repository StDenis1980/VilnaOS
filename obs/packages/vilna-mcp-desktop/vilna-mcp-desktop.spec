#
# spec file for package vilna-mcp-desktop
#
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Vilna contributors
#

# A tool program, not a library: built for the primary Python only.
%define pythons python3
# Version comes from mcp/desktop/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           vilna-mcp-desktop
Version:        0.1.0~dev0
Release:        0
Summary:        Herkos's tools for the desktop, app permissions and other apps
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
Requires:       python3 >= 3.11
# D-Bus and the accessibility bus through PyGObject.
Requires:       python3-gobject
Requires:       typelib-1_0-Atspi-2_0
# The D-Bus filter the tools sit behind (vilna-dbus-proxy-*@, ADR 0031).
Requires:       xdg-dbus-proxy
BuildArch:      noarch

%description
Three MCP servers of Vilna OS (ADR 0031), started only by Herkos, as the
person, each behind a D-Bus filter made from its Herkos manifest:
vilna-mcp-desktop (battery, notifications from Gloxi, music players, screen
lock), vilna-mcp-portals (what the Flatpak portals let each app do) and
vilna-mcp-atspi (windows of other apps through accessibility, never those
of security, password or administration apps).

%prep
%autosetup -p1

%build
%pyproject_wheel

%install
%pyproject_install
%python_expand %fdupes %{buildroot}%{$python_sitelib}

%check
%pytest

%files
%license LICENSE
%doc README.md
%{_bindir}/vilna-mcp-desktop
%{_bindir}/vilna-mcp-portals
%{_bindir}/vilna-mcp-atspi
%{python_sitelib}/vilna_mcp_desktop
%{python_sitelib}/vilna_mcp_desktop-*.dist-info

%changelog

#
# spec file for package vilna-mcp-settings
#
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Vilna contributors
#

# A tool program, not a library: built for the primary Python only.
%define pythons python3
# Version comes from mcp/settings/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           vilna-mcp-settings
Version:        0.1.0~dev0
Release:        0
Summary:        Herkos's tool for the GNOME settings Gloxi may change
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
# GSettings through PyGObject; sound through PipeWire's PulseAudio protocol.
Requires:       python3-gobject
Requires:       python3-pulsectl
Requires:       typelib-1_0-Gio-2_0
BuildArch:      noarch

%description
The MCP server "settings" of Vilna OS (ADR 0023) reads and changes a fixed
list of GNOME settings: light or dark style, accent colour, text size, night
light, do not disturb, volume, mute and sound output. Every change returns
the values from before, so Herkos can undo it. Only Herkos starts it
(vilna-mcp-settings@.service in the herkos package), as the person.

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
%{_bindir}/vilna-mcp-settings
%{python_sitelib}/vilna_mcp_settings
%{python_sitelib}/vilna_mcp_settings-*.dist-info

%changelog

#
# spec file for package vilna-mcp-installer
#
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Vilna contributors
#

# A tool program, not a library: built for the primary Python only.
%define pythons python3
# Version comes from mcp/installer/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           vilna-mcp-installer
Version:        0.1.0~dev0
Release:        0
Summary:        The Vilna installer's tools: read the computer, fill and check the plan
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
BuildRequires:  timezone
Requires:       python3 >= 3.11
# Disks (lsblk, findmnt), Wi-Fi networks (nmcli), keyboard layouts, time zones.
Requires:       NetworkManager
Requires:       timezone
Requires:       util-linux
Requires:       xkeyboard-config
BuildArch:      noarch

%description
The MCP server of the Vilna OS installer (image/docs/installer.md): it reads
the hardware, the disks, the Wi-Fi networks, the keyboard layouts and the time
zones, and changes only the installation plan. It never erases, partitions or
installs anything. Also the plan's format and its validator, which does not
depend on the model (vilna-plan-check). The installer (vilna-installer)
starts it for the conversation with Gloxi; only the live image has it.

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
%{_bindir}/vilna-mcp-installer
%{_bindir}/vilna-plan-check
%{python_sitelib}/vilna_mcp_installer
%{python_sitelib}/vilna_mcp_installer-*.dist-info

%changelog

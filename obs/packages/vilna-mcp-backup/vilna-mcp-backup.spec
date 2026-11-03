#
# spec file for package vilna-mcp-backup
#
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Vilna contributors
#

# A tool program, not a library: built for the primary Python only.
%define pythons python3
# Version comes from mcp/backup/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           vilna-mcp-backup
Version:        0.1.0~dev0
Release:        0
Summary:        Herkos's tool for the person's encrypted backups
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
Recommends:     vilna-backup
BuildArch:      noarch

%description
The MCP server "backup" of Vilna OS lets Gloxi, through Herkos, see the
state of the encrypted backups, list them, start a backup now and restore
into a new empty folder. It never sees the repository password. Only Herkos
starts it (vilna-mcp-backup@.service in the herkos package), as the person.

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
%{_bindir}/vilna-mcp-backup
%{python_sitelib}/vilna_mcp_backup
%{python_sitelib}/vilna_mcp_backup-*.dist-info

%changelog

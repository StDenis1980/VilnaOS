#
# spec file for package vilna-mcp-files
#
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Vilna contributors
#

# A tool program, not a library: built for the primary Python only.
%define pythons python3
# Version comes from mcp/files/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           vilna-mcp-files
Version:        0.1.0~dev0
Release:        0
Summary:        Herkos's tool for files in the folders the person allowed
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
BuildArch:      noarch

%description
The MCP server "files" of Vilna OS lists, searches, reads, creates, changes
and deletes text files in the folders the person allowed. Before every change
it keeps a backup, so the change can be undone; backups are kept 30 days.
Only Herkos starts it (vilna-mcp-files@.service in the herkos package), as
the person, and the server checks every path itself again.

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
%{_bindir}/vilna-mcp-files
%{python_sitelib}/vilna_mcp_files
%{python_sitelib}/vilna_mcp_files-*.dist-info

%changelog

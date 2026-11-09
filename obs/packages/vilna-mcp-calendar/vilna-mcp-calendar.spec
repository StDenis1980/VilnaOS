#
# spec file for package vilna-mcp-calendar
#
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Vilna contributors
#

# A tool program, not a library: built for the primary Python only.
%define pythons python3
# Version comes from mcp/calendar/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           vilna-mcp-calendar
Version:        0.1.0~dev0
Release:        0
Summary:        Herkos's tool for reading the calendars of GNOME
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
# The calendars of GNOME through Evolution Data Server, with PyGObject.
Requires:       evolution-data-server
Requires:       python3-gobject
Requires:       typelib-1_0-ECal-2_0
Requires:       typelib-1_0-EDataServer-1_2
Requires:       typelib-1_0-ICalGLib-3_0
BuildArch:      noarch

%description
The MCP server "calendar" of Vilna OS (ADR 0023) reads the calendars GNOME
Calendar and GNOME Online Accounts show, through Evolution Data Server. It
works out "today", "this week" and the like itself, and writes nothing.
Only Herkos starts it (vilna-mcp-calendar@.service in the herkos package),
as the person.

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
%{_bindir}/vilna-mcp-calendar
%{python_sitelib}/vilna_mcp_calendar
%{python_sitelib}/vilna_mcp_calendar-*.dist-info

%changelog

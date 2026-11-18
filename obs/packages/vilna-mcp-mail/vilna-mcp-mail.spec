#
# spec file for package vilna-mcp-mail
#
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Vilna contributors
#

# A tool program, not a library: built for the primary Python only.
%define pythons python3
# Version comes from mcp/mail/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           vilna-mcp-mail
Version:        0.1.0~dev0
Release:        0
Summary:        Herkos's tool for the mail of GNOME Online Accounts
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
# Accounts and their sign-in from GNOME Online Accounts, with PyGObject.
# IMAP and SMTP themselves are Python's own imaplib and smtplib.
Requires:       gnome-online-accounts
Requires:       python3-gobject
Requires:       typelib-1_0-Goa-1_0
BuildArch:      noarch

%description
The MCP server "mail" of Vilna OS (ADR 0030) reads the mail of the accounts
in GNOME Online Accounts over IMAP, saves drafts and sends over SMTP, only
over encrypted connections. Herkos asks the person before every message
that goes out, showing all of it.
Only Herkos starts it (vilna-mcp-mail@.service in the herkos package),
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
%{_bindir}/vilna-mcp-mail
%{python_sitelib}/vilna_mcp_mail
%{python_sitelib}/vilna_mcp_mail-*.dist-info

%changelog

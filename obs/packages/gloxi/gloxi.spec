#
# spec file for package gloxi
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

# Gloxi is an application, not a library: build for the primary Python only.
%define pythons python3
# Version comes from gloxi/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           gloxi
Version:        0.1.0~a1
Release:        0
Summary:        AI assistant of Vilna OS
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        %{name}-%{version}.tar.gz
BuildRequires:  %{python_module base >= 3.11}
BuildRequires:  %{python_module hatchling >= 1.26}
BuildRequires:  %{python_module httpx >= 0.27}
BuildRequires:  %{python_module pip}
BuildRequires:  %{python_module pytest >= 8}
BuildRequires:  %{python_module vilna-privacy-gateway}
BuildRequires:  %{python_module wheel}
BuildRequires:  fdupes
BuildRequires:  python-rpm-macros
BuildRequires:  sysuser-tools
BuildRequires:  systemd-rpm-macros
%sysusers_requires
Requires:       python3-httpx >= 0.27
# Masks personal data before it goes to the cloud (on by default).
Requires:       python3-vilna-privacy-gateway
# Everything Gloxi does goes through the system Herkos (herkosd); the
# client library is in herkos-agent.
Requires:       herkos
Requires:       herkos-agent
# useradd/userdel for vilna-people-sync.
Requires:       shadow
# The shared local model (vilna-llm.service).
Requires:       llamacpp
%{?systemd_ordering}
BuildArch:      noarch

%description
Gloxi is the AI assistant of Vilna OS. It answers simple and private
requests with a small local model and does everything on the system only
through the Herkos permission gateway.

This package is the Gloxi service (ADR 0025): each person's Gloxi runs
as its own system user (gloxi@<uid>.service) and talks to the system
Herkos; one local model (vilna-llm.service) serves everybody, and
vilna-people-sync gives every person on the computer their Gloxi.

%package session
Summary:        Gloxi window in the person's session
License:        GPL-3.0-or-later
Requires:       %{name} = %{version}-%{release}
# The chat window (gloxi-gtk): PyGObject with GTK 4 and libadwaita.
Requires:       python3-gobject
Requires:       typelib(Adw) = 1
Requires:       typelib(Gtk) = 4.0
BuildArch:      noarch

%description session
The Gloxi window (gloxi-gtk): the side panel, the hotkey and the
conversation. It only shows the person's Gloxi service and passes on
what the person types.

%prep
%autosetup -p1

%build
%pyproject_wheel

%sysusers_generate_pre systemd/gloxi.sysusers gloxi gloxi.conf

%install
%pyproject_install
%python_expand %fdupes %{buildroot}%{$python_sitelib}
install -Dm0644 data/io.github.stdenis1980.Gloxi.desktop \
  %{buildroot}%{_datadir}/applications/io.github.stdenis1980.Gloxi.desktop
for unit in gloxi@.socket gloxi@.service vilna-llm.service \
            vilna-people-sync.service vilna-people-sync.path; do
  install -Dm0644 systemd/$unit %{buildroot}%{_unitdir}/$unit
done
install -Dm0644 systemd/gloxi.sysusers %{buildroot}%{_sysusersdir}/gloxi.conf
install -d %{buildroot}%{_datadir}/vilna/models

%check
%pytest

%pre -f gloxi.pre
%service_add_pre vilna-people-sync.service vilna-people-sync.path vilna-llm.service

%post
%service_add_post vilna-people-sync.service vilna-people-sync.path vilna-llm.service

%preun
%service_del_preun vilna-people-sync.service vilna-people-sync.path vilna-llm.service

%postun
%service_del_postun vilna-people-sync.service vilna-people-sync.path vilna-llm.service

%files
%license LICENSE
%doc README.md
%{_bindir}/gloxi
%{_bindir}/gloxid
%{_bindir}/vilna-people-sync
%{_bindir}/gloxi-bench
%{_unitdir}/gloxi@.socket
%{_unitdir}/gloxi@.service
%{_unitdir}/vilna-llm.service
%{_unitdir}/vilna-people-sync.service
%{_unitdir}/vilna-people-sync.path
%{_sysusersdir}/gloxi.conf
%dir %{_datadir}/vilna
%dir %{_datadir}/vilna/models
%{python_sitelib}/gloxi
%{python_sitelib}/gloxi-*.dist-info

%files session
%license LICENSE
%{_bindir}/gloxi-gtk
%{_datadir}/applications/io.github.stdenis1980.Gloxi.desktop

%changelog

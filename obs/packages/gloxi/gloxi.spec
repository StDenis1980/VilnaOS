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
Version:        0.1.0~dev0
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
BuildRequires:  %{python_module wheel}
BuildRequires:  fdupes
BuildRequires:  python-rpm-macros
Requires:       python3-httpx >= 0.27
# llama-server is optional: Gloxi can talk to any OpenAI-compatible server.
Recommends:     llama.cpp
BuildArch:      noarch

%description
Gloxi is the AI assistant of Vilna OS. It answers simple and private
requests with a small local model and does everything on the system only
through the Herkos permission gateway.

This 0.1 preview is a terminal chat with a local model served by
llama-server (or any OpenAI-compatible API).

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
%{_bindir}/gloxi
%{python_sitelib}/gloxi
%{python_sitelib}/gloxi-*.dist-info

%changelog

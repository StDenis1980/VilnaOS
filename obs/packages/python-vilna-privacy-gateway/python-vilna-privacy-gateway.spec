#
# spec file for package python-vilna-privacy-gateway
#
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Vilna contributors
#

# Only Gloxi uses it for now, and Gloxi is built for the primary Python.
%define pythons python3
# Version comes from privacy-gateway/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           python-vilna-privacy-gateway
Version:        0.1.0~dev0
Release:        0
Summary:        On-device masking of personal data before it reaches a cloud model
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
BuildArch:      noarch
%python_subpackages

%description
The privacy gateway of Vilna OS finds personal and secret data (contacts,
document and account numbers, keys, labelled names and addresses) in text
on the device, replaces it with numbered placeholders before a request
goes to a cloud model, and puts the real values back into the answer.

%prep
%autosetup -p1

%build
%pyproject_wheel

%install
%pyproject_install
%python_expand %fdupes %{buildroot}%{$python_sitelib}

%check
%pytest

%files %{python_files}
%license LICENSE
%doc README.md
%{python_sitelib}/privacy_gateway
%{python_sitelib}/vilna_privacy_gateway-*.dist-info

%changelog

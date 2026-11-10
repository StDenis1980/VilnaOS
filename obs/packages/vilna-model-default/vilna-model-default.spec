#
# spec file for package vilna-model-default
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

%define gguf Qwen3-4B-Q4_K_M.gguf
%define sha256 7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5

Name:           vilna-model-default
Version:        2025.04
Release:        0
Summary:        Default local language model of Vilna (Qwen3 4B, Q4_K_M)
# The packaging is GPL-3.0-or-later; the weights are Qwen3's, Apache-2.0.
License:        Apache-2.0
URL:            https://huggingface.co/Qwen/Qwen3-4B-GGUF
# Fetched by OBS from the URL above and checked against %%{sha256} (_service).
Source0:        %{gguf}
Source1:        LICENSE-Qwen3
BuildArch:      noarch
Provides:       vilna-model = %{version}

%description
The weights of the model Gloxi uses on this computer: Qwen3 4B, quantised
to Q4_K_M (about 2.5 GB), chosen in ADR 0008. vilna-llm.service serves them
for everybody on the computer, so the installer and the first conversation
work without a network (ADR 0006, ADR 0025).

%prep
cp %{SOURCE1} LICENSE
echo "%{sha256}  %{SOURCE0}" | sha256sum -c -

%build

%install
install -Dm0644 %{SOURCE0} %{buildroot}%{_datadir}/vilna/models/%{gguf}
ln -s %{gguf} %{buildroot}%{_datadir}/vilna/models/default.gguf
# A stable path and sum for whoever copies the weights elsewhere (the
# installer copies them from the live medium to the new disk).
echo "%{sha256}  %{gguf}" > %{buildroot}%{_datadir}/vilna/models/default.gguf.sha256

%files
%license LICENSE
%dir %{_datadir}/vilna
%dir %{_datadir}/vilna/models
%{_datadir}/vilna/models/%{gguf}
%{_datadir}/vilna/models/default.gguf
%{_datadir}/vilna/models/default.gguf.sha256

%changelog

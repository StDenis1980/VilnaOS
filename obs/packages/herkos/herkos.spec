#
# spec file for package herkos
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

# The Python part (herkos-agent) is an application: primary Python only.
%define pythons python3
# Version comes from herkos/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           herkos
Version:        0.1.0~dev0
Release:        0
Summary:        Permission gate of Vilna OS
# herkosd links crates under MIT, Apache-2.0, Unicode-3.0, Unlicense, Zlib (vendor.tar.xz).
License:        GPL-3.0-or-later AND (Apache-2.0 OR MIT) AND Unicode-3.0
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        %{name}-%{version}.tar.gz
# The Rust crates of herkos/rs/Cargo.lock: OBS builds without network.
Source1:        vendor.tar.xz
BuildRequires:  cargo >= 1.85
BuildRequires:  %{python_module base >= 3.11}
BuildRequires:  %{python_module hatchling >= 1.26}
BuildRequires:  %{python_module pip}
BuildRequires:  %{python_module wheel}
BuildRequires:  fdupes
BuildRequires:  python-rpm-macros
BuildRequires:  sysuser-tools
BuildRequires:  systemd-rpm-macros
%sysusers_requires
# 50-herkos.rules: herkosd may start only its tool units.
Requires:       polkit
# The tools run as the person (vilna-mcp-*@.service); packaged separately.
Recommends:     vilna-mcp-files
Recommends:     vilna-mcp-backup
%{?systemd_ordering}

%description
Herkos is the permission gate of Vilna OS: every action of the Gloxi
assistant goes through it. It decides by risk level, asks the person when
needed, keeps a tamper-evident journal (Mitos) and checks what Gloxi reads
for prompt injection (Keros).

This package is the system service herkosd: one for the whole computer,
as the system user herkos, with separate permissions and journals for
each person.

%package agent
Summary:        Confirmation windows of the Herkos permission gate
License:        GPL-3.0-or-later
Requires:       python3-gobject
Requires:       typelib(Adw) = 1
Requires:       typelib(Gtk) = 4.0
BuildArch:      noarch

%description agent
The session agent of Herkos: shows the questions of the system herkosd on
the person's screen (herkos-agent, a user service) in Herkos's own
window. The "Permissions" window (herkos-permissions) shows what Gloxi
did, undoes a change, and takes back a permission. It also contains Herkos-lite, the preview gate that runs in the
person's session.

%package -n herkos-manifests
Summary:        Herkos manifests for programs Gloxi may use
License:        GPL-3.0-or-later
Requires:       %{name} = %{version}-%{release}
Requires:       flatpak
BuildArch:      noarch

%description -n herkos-manifests
The catalogue of programs from Flatpak that the Gloxi assistant may use
through Herkos (ADR 0017): for each one, how herkosd starts it in its own
sandbox and the risk level of each of its actions. First the five Tinta
Office programs. Each program stays off until the person turns it on in
Gloxi's settings.

%prep
%autosetup -p1 -a1
# Cargo looks for its config from the working directory up, not next to
# --manifest-path, so the config goes here, at the top of the sources.
mkdir -p .cargo
cat > .cargo/config.toml <<'CARGO'
[source.crates-io]
replace-with = "vendored-sources"

[source.vendored-sources]
directory = "vendor"
CARGO

%build
cargo build --manifest-path rs/Cargo.toml --release --offline --locked -p herkosd
%pyproject_wheel
%sysusers_generate_pre systemd/herkos.sysusers herkos herkos.conf

%install
install -Dm0755 rs/target/release/herkosd %{buildroot}%{_libexecdir}/herkos/herkosd
install -Dm0644 systemd/herkosd.socket %{buildroot}%{_unitdir}/herkosd.socket
install -Dm0644 systemd/herkosd.service %{buildroot}%{_unitdir}/herkosd.service
install -Dm0644 systemd/vilna-mcp-files@.service %{buildroot}%{_unitdir}/vilna-mcp-files@.service
install -Dm0644 systemd/vilna-mcp-backup@.service %{buildroot}%{_unitdir}/vilna-mcp-backup@.service
install -Dm0644 systemd/50-herkos.rules %{buildroot}%{_datadir}/polkit-1/rules.d/50-herkos.rules
install -Dm0644 systemd/herkos.sysusers %{buildroot}%{_sysusersdir}/herkos.conf
install -Dm0644 systemd/system.toml %{buildroot}%{_sysconfdir}/herkos/system.toml
install -Dm0644 systemd/herkos-agent.service %{buildroot}%{_userunitdir}/herkos-agent.service
# The «Дозволи» window (herkos-permissions) in the app grid.
install -Dm0644 data/io.github.stdenis1980.VilnaPermissions.desktop %{buildroot}%{_datadir}/applications/io.github.stdenis1980.VilnaPermissions.desktop
install -Dm0644 data/vilna-permissions.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/vilna-permissions.svg
install -Dm0644 data/vilna-permissions-symbolic.svg %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps/vilna-permissions-symbolic.svg
# The catalogue (herkos/catalogue) and, for each program in it, the unit
# that runs it for a person, made from its manifest.
install -d %{buildroot}%{_datadir}/herkos/manifests
for manifest in catalogue/*.toml; do
  install -m0644 "$manifest" %{buildroot}%{_datadir}/herkos/manifests/
  program=$(sed -n 's/^program = "\(vilna-mcp-ext-[a-z0-9-]*\)"$/\1/p' "$manifest")
  rs/target/release/herkosd ext-unit "$manifest" > %{buildroot}%{_unitdir}/"$program"@.service
done
%pyproject_install
%python_expand %fdupes %{buildroot}%{$python_sitelib}

%check
cargo test --manifest-path rs/Cargo.toml --release --offline --locked
%{buildroot}%{_libexecdir}/herkos/herkosd --version

%pre -f herkos.pre
%service_add_pre herkosd.socket herkosd.service

%post
%service_add_post herkosd.socket herkosd.service

%preun
%service_del_preun herkosd.socket herkosd.service

%postun
%service_del_postun herkosd.socket herkosd.service

%post agent
%systemd_user_post herkos-agent.service

%preun agent
%systemd_user_preun herkos-agent.service

%files
%license LICENSE
%doc README.md
%dir %{_libexecdir}/herkos
%{_libexecdir}/herkos/herkosd
%{_unitdir}/herkosd.socket
%{_unitdir}/herkosd.service
%{_unitdir}/vilna-mcp-files@.service
%{_unitdir}/vilna-mcp-backup@.service
%{_datadir}/polkit-1/rules.d/50-herkos.rules
%{_sysusersdir}/herkos.conf
%dir %{_sysconfdir}/herkos
%config(noreplace) %{_sysconfdir}/herkos/system.toml

%files -n herkos-manifests
%license LICENSE
%dir %{_datadir}/herkos
%{_datadir}/herkos/manifests
%{_unitdir}/vilna-mcp-ext-*@.service

%files agent
%license LICENSE
%{_bindir}/herkos
%{_bindir}/herkos-agent
%{_bindir}/herkos-dialog
%{_bindir}/herkos-permissions
%{_userunitdir}/herkos-agent.service
%{_datadir}/applications/io.github.stdenis1980.VilnaPermissions.desktop
%{_datadir}/icons/hicolor/scalable/apps/vilna-permissions.svg
%{_datadir}/icons/hicolor/symbolic/apps/vilna-permissions-symbolic.svg
%{python_sitelib}/herkos
%{python_sitelib}/herkos-*.dist-info

%changelog

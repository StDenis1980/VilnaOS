#
# spec file for package mullvad-vpn
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

# Mullvad's own RPM for Fedora, repackaged for Vilna (ADR 0027). The files
# are Mullvad's, unchanged; the scriptlets are ours. Theirs enable and start
# the daemon at once, make mullvad-exclude setuid, and on removal delete
# ~/.config/Mullvad VPN of every user and unregister the device through the
# Mullvad API. Here the daemon stays off until the person chooses Mullvad
# (vilna-presets does not enable it), nothing is setuid (split tunnelling is
# not offered), and removal leaves people's settings alone. The app moves
# from /opt/Mullvad VPN to %%{_libdir}/mullvad-vpn: on Vilna /opt is its own
# subvolume outside the root snapshot, so a transactional update would
# write the app where a rollback cannot reach it.

%define upstream_rpm MullvadVPN-%{version}_x86_64.rpm
%define sha256 6cd0de1c42527b67870e2991f9c0a0399ed6a565f866476536a4d240b289af74
%define appdir %{_libdir}/mullvad-vpn
%define services mullvad-daemon.service mullvad-early-boot-blocking.service

# Prebuilt binaries: no debug package, no stripping, and the libraries the
# Electron app bundles are neither provided to nor required from the system.
%global debug_package %{nil}
%global __strip /bin/true
%global __provides_exclude_from ^%{_libdir}/mullvad-vpn/.*$
%global __requires_exclude ^lib(ffmpeg|EGL|GLESv2|vk_swiftshader|vulkan)\\.so.*$

Name:           mullvad-vpn
Version:        2026.5
Release:        0
Summary:        Mullvad VPN app and daemon (Mullvad's build, Vilna scriptlets)
License:        GPL-3.0-or-later
URL:            https://mullvad.net
# Fetched by OBS from repository.mullvad.net and checked against %%{sha256} (_service).
Source0:        %{upstream_rpm}
BuildRequires:  cpio
BuildRequires:  rpm
BuildRequires:  systemd-rpm-macros
%{?systemd_ordering}
ExclusiveArch:  x86_64

%description
The Mullvad VPN app and its system service, as Mullvad builds them. In
Vilna it is the "most anonymity" choice beside Proton VPN: the person
enters a Mullvad account number or activation code in this app only,
never in Vilna or Gloxi. The service stays off until the person chooses
Mullvad, and the Herkos kill switch (vilna-netguard) works with it.

%prep
echo "%{sha256}  %{SOURCE0}" | sha256sum -c -
mkdir -p tree
cd tree
rpm2cpio %{SOURCE0} | cpio -idm --quiet

%build

%install
cp -a tree/. %{buildroot}/
# Out of /opt (see the top): the app, the links to it and every text file
# that names its folder. The daemon finds its resources next to its own
# executable, and MULLVAD_RESOURCE_DIR (the drop-in below) says it as well.
mkdir -p %{buildroot}%{_libdir}
mv "%{buildroot}/opt/Mullvad VPN" %{buildroot}%{appdir}
rmdir %{buildroot}/opt
find %{buildroot} -type l | while read -r link; do
  target=$(readlink "$link")
  case "$target" in
    "/opt/Mullvad VPN"*) ln -sfn "%{appdir}${target#/opt/Mullvad VPN}" "$link" ;;
  esac
done
# Whatever the space is written as ("/opt/Mullvad VPN", "\ ", systemd's
# "\x20"), the new folder has none.
grep -rlIZ "/opt/Mullvad" %{buildroot} | xargs -0 -r sed -i 's|/opt/Mullvad[^/"]\{1,4\}VPN|%{appdir}|g'
# Absolute interpreters, not /usr/bin/env.
grep -rlIZ '^#!/usr/bin/env bash' %{buildroot} | xargs -0 -r sed -i '1s|^#!/usr/bin/env bash|#!/bin/bash|'
mkdir -p %{buildroot}%{_unitdir}/mullvad-daemon.service.d
cat > %{buildroot}%{_unitdir}/mullvad-daemon.service.d/50-vilna.conf <<'CONF'
# Vilna: the app lives in %{appdir}, not /opt (mullvad-vpn.spec).
[Service]
Environment="MULLVAD_RESOURCE_DIR=%{appdir}/resources"
CONF
# setuid only through the permissions package on openSUSE; not offered.
find %{buildroot} -type f -perm /6000 -exec chmod ug-s {} +
# Mullvad's AppArmor profile: Vilna uses SELinux.
rm -rf %{buildroot}%{_sysconfdir}/apparmor.d
# Nothing may still point into /opt.
if grep -rnI "/opt/Mullvad" %{buildroot}; then echo "still names /opt" >&2; exit 1; fi
# The file list, from what Mullvad ships (paths with spaces quoted). Every
# directory is owned too, except the system's own, so OBS's file-list check
# finds no directory without an owner (completion folders and the like).
(cd %{buildroot}
 find . -mindepth 1 \( -type f -o -type l \) -printf '"/%%P"\n'
 find . -mindepth 1 -type d -printf '%%P\n' \
   | grep -vxE 'etc|usr|usr/bin|usr/sbin|usr/lib|usr/lib64|usr/share|usr/lib/systemd|usr/lib/systemd/system|usr/share/applications|usr/share/icons|usr/share/doc|usr/share/licenses|usr/share/man|usr/share/man/man1' \
   | sed 's|.*|%%dir "/&"|') > %{_builddir}/files.list

%pre
%service_add_pre %{services}

%post
%service_add_post %{services}

%preun
%service_del_preun %{services}

%postun
%service_del_postun %{services}

%files -f %{_builddir}/files.list

%changelog

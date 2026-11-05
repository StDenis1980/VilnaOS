#
# spec file for package patterns-vilna
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

Name:           patterns-vilna
Version:        0.3
Release:        0
Summary:        Patterns for Vilna OS
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
ExclusiveArch:  x86_64

%description
The patterns of Vilna OS. vilna_base is the whole Vilna system: the
openSUSE Aeon base with Vilna branding and the Vilna components.

%package base
Summary:        Vilna OS base system
Group:          Metapackages
Provides:       pattern() = vilna_base
Provides:       pattern-icon() = pattern-generic
Provides:       pattern-order() = 1000
Provides:       pattern-visible()
# Vilna branding and components (image/docs/composition.md, section 4).
# Vilna-release provides distribution-release, which Aeon's packages need.
Requires:       Vilna-release
Requires:       distribution-logos-Vilna
Requires:       vilna-presets
Requires:       herkos
Requires:       herkos-agent
# Programs Gloxi may use (Tinta Office); each stays off until turned on.
Requires:       herkos-manifests
Requires:       gloxi
Requires:       vilna-mcp-files
Requires:       vilna-mcp-backup
Requires:       vilna-flatpak-defaults
Requires:       gnome-shell-extension-vilna
Requires:       vilna-software-picks
# Not yet packaged, added here once they build on OBS:
#   vilna-model-default   default model weights (ADR 0006, 0008)
#
# The requirements of patterns-aeon-base 5.0 (aeon_base, Tumbleweed of
# 2026-11-05), copied because that pattern cannot be installed with
# Vilna branding: it requires Aeon-release and
# distribution-logos-openSUSE-Aeon, which our Vilna-release and
# distribution-logos-Vilna replace, and malcontent-control, which
# Tumbleweed no longer has. Those three are left out, as is
# distribution-release (Vilna-release above), and
# systemd-repart-branding-Aeon: its repart.d is made for Aeon's tik
# installer and, on a ready KIWI disk, makes systemd-repart fail the
# first boot (no free space for the partitions it wants). The Aeon branding that
# does not conflict (gnome-branding-Aeon, systemd-presets-branding-Aeon
# and the like) stays until the *-branding-Vilna packages exist; the
# image's config.sh lists what is left. Compare with
# `zypper info --requires patterns-aeon-base` when Aeon changes.
Requires:       coreutils
Requires:       aaa_base
Requires:       bash-completion
Requires:       systemd
Requires:       glibc
Requires:       xorg-x11-server
Requires:       xorg-x11-fonts-core
Requires:       ghostscript-fonts-std
Requires:       kernel-default
Requires:       bash
Requires:       util-linux
Requires:       gstreamer-plugins-bad
Requires:       NetworkManager
Requires:       shadow
Requires:       nautilus
Requires:       policycoreutils
Requires:       at-spi2-core
Requires:       udisks2
Requires:       curl
Requires:       yelp
Requires:       snapper
Requires:       gstreamer-plugins-good
Requires:       gnome-system-monitor
Requires:       gnome-bluetooth
Requires:       avahi
Requires:       gnome-online-accounts
Requires:       gnome-disk-utility
Requires:       orca
Requires:       xdg-user-dirs-gtk
Requires:       fprintd
Requires:       gnome-software
Requires:       wget
Requires:       gnome-initial-setup
Requires:       gnome-color-manager
Requires:       xdg-utils
Requires:       branding-openSUSE
Requires:       system-config-printer-common
Requires:       sudo
Requires:       libgnomesu
Requires:       samba
Requires:       polkit-gnome
Requires:       gnome-console
Requires:       gnome-remote-desktop
Requires:       nautilus-share
Requires:       rpm
Requires:       xdg-desktop-portal-gnome
Requires:       procps
Requires:       unzip
Requires:       cups-pk-helper
Requires:       gzip
Requires:       iproute2
Requires:       ca-certificates
Requires:       openssh
Requires:       xdg-desktop-portal-gtk
Requires:       ca-certificates-mozilla
Requires:       btrfsprogs
Requires:       timezone
Requires:       tpm2-0-tss
Requires:       zypper
Requires:       desktop-file-utils
Requires:       ghostscript
Requires:       cups
Requires:       podman
Requires:       selinux-policy-targeted
Requires:       pam
Requires:       flatpak
Requires:       adwaita-icon-theme
Requires:       upower
Requires:       dosfstools
Requires:       libnss_usrfiles2
Requires:       transactional-update
Requires:       audit
Requires:       filesystem
Requires:       pam-config
Requires:       iputils
Requires:       policycoreutils-python-utils
Requires:       glibc-locale
Requires:       sane-backends
Requires:       dejavu-fonts
Requires:       hostname
Requires:       terminfo-base
Requires:       /usr/bin/gzip
Requires:       google-roboto-fonts
Requires:       kdump
Requires:       less
Requires:       ethtool
Requires:       pipewire-pulseaudio
Requires:       chrony
Requires:       gjs
Requires:       glibc-locale-base
Requires:       selinux-tools
Requires:       tpm2.0-tools
Requires:       busybox
Requires:       health-checker
Requires:       hwinfo
Requires:       ntfs-3g
Requires:       power-profiles-daemon
Requires:       cifs-utils
Requires:       cups-filters
Requires:       microos-tools
Requires:       ntfsprogs
Requires:       polkit-default-privs
Requires:       user(nobody)
Requires:       container-selinux
Requires:       distrobox
Requires:       gnome-keyring-pam
Requires:       systemd-boot
Requires:       glycin-loaders
Requires:       group(nobody)
Requires:       gvfs-backends
Requires:       noto-sans-fonts
Requires:       bolt
Requires:       coreutils-systemd
Requires:       efibootmgr
Requires:       google-droid-fonts
Requires:       irqbalance
Requires:       openSUSE-build-key
Requires:       /usr/bin/hostname
Requires:       adobe-sourcecodepro-fonts
Requires:       btrfsmaintenance
Requires:       exfatprogs
Requires:       gsettings-backend-dconf
Requires:       libmbim
Requires:       mokutil
Requires:       wtmpdb
Requires:       fcoe-utils
Requires:       google-opensans-fonts
Requires:       gpgme
Requires:       shim
Requires:       switcheroo-control
Requires:       xf86-input-libinput
Requires:       OpenPrintingPPDs
Requires:       adobe-sourcesanspro-fonts
Requires:       branding
Requires:       build-key
Requires:       fprintd-pam
Requires:       gcr-ssh-agent
Requires:       gstreamer-plugin-pipewire
Requires:       hicolor-icon-theme-branding-openSUSE
Requires:       iio-sensor-proxy
Requires:       lastlog2
Requires:       noto-coloremoji-fonts
Requires:       pipewire-alsa
Requires:       python3-nautilus
Requires:       qemu-guest-agent
Requires:       system-config-printer-dbus-service
Requires:       vim-small
Requires:       zypper-needs-restarting
Requires:       NetworkManager-bluetooth
Requires:       adobe-sourceserifpro-fonts
Requires:       adwaita-fonts
Requires:       bluez-firmware
Requires:       canberra-gtk-play
Requires:       gcr-ssh-askpass
Requires:       gnome-user-docs
Requires:       google-carlito-fonts
Requires:       kernel-firmware-all
Requires:       systemd-coredump
Requires:       systemd-icon-branding-openSUSE
Requires:       transactional-update-zypp-config
Requires:       udev-configure-printer
Requires:       yast2-logs
Requires:       NetworkManager-openconnect-gnome
Requires:       NetworkManager-openvpn-gnome
Requires:       NetworkManager-wifi
Requires:       alsa-ucm-conf
Requires:       combustion
Requires:       gcr3-ssh-askpass
Requires:       gnome-backgrounds
Requires:       health-checker-plugins-MicroOS
Requires:       hplip-hpijs
Requires:       libvulkan_intel
Requires:       libvulkan_radeon
Requires:       nautilus-extension-seahorse
Requires:       sdbootutil-snapper
Requires:       spice-vdagent
Requires:       steam-devices
Requires:       system-group-wheel
Requires:       systemd-zram-service
Requires:       transactional-update-notifier
Requires:       uefi_mbr
Requires:       bluez-auto-enable-devices
Requires:       bluez-cups
Requires:       dracut-pcr-signature
Requires:       epson-inkjet-printer-escpr
Requires:       ghostscript-fonts-other
Requires:       gnome-control-center-color
Requires:       gnome-control-center-goa
Requires:       gnome-control-center-user-faces
Requires:       gnome-session-wayland
Requires:       gtk2-branding-openSUSE
Requires:       gtk3-branding-openSUSE
Requires:       gvfs-fuse
Requires:       libinput-udev
Requires:       openssh-askpass-gnome
Requires:       pinentry-gnome3
Requires:       seahorse-daemon
Requires:       sof-firmware
Requires:       systemd-experimental
Requires:       ucode-amd
Requires:       ucode-intel
Requires:       zypp-no-recommends
Requires:       bluez-obexd
Requires:       brltty-driver-speech-dispatcher
Requires:       gdm-branding-Aeon
Requires:       gnome-branding-Aeon
Requires:       gnome-control-center-users
Requires:       gnome-menus-branding-openSUSE
Requires:       gnome-session-default-session
Requires:       gnome-shell-calendar
Requires:       gsf-office-thumbnailer
Requires:       gtk4-branding-openSUSE
Requires:       gvfs-backend-afc
Requires:       gvfs-backend-goa
Requires:       noto-emoji-fonts
Requires:       printer-driver-brlaser
Requires:       rsvg-thumbnailer
Requires:       speech-dispatcher-module-espeak
Requires:       systemd-presets-branding-Aeon
Requires:       zypp-excludedocs
Requires:       zypp-no-multiversion
Requires:       aeon-check
Requires:       chrome-gnome-shell
Requires:       gnome-shell-search-provider-nautilus
Requires:       gvfs-backend-samba
Requires:       ignition-dracut
Requires:       jxl-thumbnailer
Requires:       sdbootutil-rpm-scriptlets
Requires:       x86_64_v3-branding-Aeon

%description base
The Vilna OS base system: openSUSE Aeon (GNOME, transactional-update,
snapper, SELinux, Flatpak) with Vilna branding, the Gloxi assistant,
the Herkos permission gate, the Vilna MCP tools and the default
Flatpak apps.

%prep

%build

%install
mkdir -p %{buildroot}%{_docdir}/patterns-vilna
echo 'This file marks the pattern vilna_base to be installed.' \
    > %{buildroot}%{_docdir}/patterns-vilna/vilna_base.txt

%files base
%dir %{_docdir}/patterns-vilna
%{_docdir}/patterns-vilna/vilna_base.txt

%changelog

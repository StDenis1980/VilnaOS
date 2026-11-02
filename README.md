<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/design/logo/Vilna_logo_dark-bg.svg">
    <img src="docs/design/logo/Vilna_logo_color.svg" alt="Vilna" width="180">
  </picture>
</p>

# Vilna

**English** · [Українська](README.uk.md) · [Español](README.es.md)

Vilna is a free Linux operating system for everyone in Europe, with a built-in AI assistant, **Gloxi**, and a security layer, **Herkos**, that every action of the assistant has to pass through.

*Free as in freedom.*

> **Status: early development.** Nothing has been released yet. The first release, **Gloxi 0.1 preview** for ordinary Linux desktops with GNOME, is planned for the end of January 2027. You can already build and run it from source: see [Getting started](docs/getting-started.md).

## What Vilna is

- **Base:** openSUSE Aeon (GNOME) first, Kalpa (KDE) later. The system is immutable, updates are transactional and can be rolled back with btrfs snapshots. Fedora Atomic (bootc) is the fallback.
- **Gloxi** is the assistant. It runs as a system service, can work with files, apps and settings, keeps your calendar and helps organise your time. It never touches the system directly.
- **Herkos** is the security layer. Every action Gloxi wants to take goes through one permission gate, which checks the risk level, asks you when needed and writes a tamper-evident journal. It also includes a prompt-injection filter (Keros) and undo through journal and snapshots (Mitos). Specification: [`herkos/docs/permissions.md`](herkos/docs/permissions.md).
- **Privacy gateway.** Before anything goes to a cloud model, personal data (email addresses, phone numbers, IBANs, document numbers, keys and passwords) is replaced on your device with placeholders like `[EMAIL_1]`, and the real values are put back into the answer.
- **Tools** are MCP servers: files, apps, settings, CalDAV calendar, IMAP mail.
- **Models.** A router sends private and simple requests to a small local model, and complex ones to the cloud only if you turn the cloud on and give your own key. The default local model is Qwen3 4B ([ADR 0008](docs/adr/0008-default-local-model.md)); any OpenAI-compatible server can replace it.
- **Minimum hardware:** Intel Core 8th gen or AMD Ryzen 2000, 8 GB RAM. 16 GB is recommended for comfortable work with the local model.

## Try Gloxi 0.1 preview

Gloxi currently runs as a development build on openSUSE Aeon, Fedora, Ubuntu or any other Linux with Python 3.11 or newer. In short:

```sh
llama-server -hf Qwen/Qwen3-4B-GGUF:Q4_K_M --port 8080 -c 4096 --jinja \
  --chat-template-kwargs '{"enable_thinking":false}'

git clone https://github.com/StDenis1980/VilnaOS.git
cd VilnaOS/gloxi
uv sync
uv run gloxi
```

The full guide covers installing llama.cpp and uv, the GTK window, working with files through Herkos, an optional cloud model, and common problems: [docs/getting-started.md](docs/getting-started.md).

## Roadmap

| When | What |
| --- | --- |
| January 2027 | **Gloxi 0.1 preview**: chat with a local model, model router, privacy gateway, Herkos-lite with a files tool |
| February to March 2027 | Gloxi 0.2: calendar, GNOME settings, side panel window, Flathub |
| April to September 2027 | First Vilna image on openSUSE Aeon, Gloxi as a system service, system Herkos in Rust |
| by March 2028 | Vilna alpha |
| 2028 | Public beta |

Dates are a plan, not a promise. Details: [`docs/roadmap.md`](docs/roadmap.md) (in Ukrainian).

## Repository layout

This is a monorepo: each component lives in its own top-level folder with its own `README.md`.

| Folder | Contents |
| --- | --- |
| [`gloxi/`](gloxi/) | The Gloxi assistant: terminal chat, GTK window, model router, files tool client |
| [`herkos/`](herkos/) | Security layer: permission gate, Keros, Mitos. Herkos-lite (Python) for Gloxi 0.1 |
| [`privacy-gateway/`](privacy-gateway/) | Privacy gateway library: reversible on-device masking (Apache-2.0) |
| [`mcp/`](mcp/) | MCP tool servers, starting with files (Apache-2.0) |
| [`sdk/`](sdk/) | SDK for third-party developers (Apache-2.0) |
| [`image/`](image/) | The Vilna system image on openSUSE Aeon / Kalpa |
| [`backup/`](backup/) | Encrypted backups of Vilna data (restic, keys in Herkos); prototype `vilna-backup` |
| [`obs/`](obs/) | Vilna project and RPM packages in the Open Build Service |
| [`docs/`](docs/) | Roadmap, [architecture decisions](docs/adr/), [benchmarks](docs/benchmarks.md), [design brief](docs/design/brief.md) |

Most project documents are in Ukrainian for now. The entry points, this README and the getting-started guide, are in English, Ukrainian and Spanish.

## Contributing

The project is not accepting outside code contributions yet: a contributor licence agreement (CLA) has to be in place first. Bug reports and ideas are welcome as issues.

## Licence

[GPL-3.0-or-later](LICENSE) by default: the OS, Gloxi and Herkos. Shared components meant to be used outside Vilna (privacy gateway, MCP servers, SDK) are Apache-2.0, each with its own `LICENSE` file. Details: [`LICENSING.md`](LICENSING.md).

# Getting started: build and run Gloxi 0.1 preview

**English** · [Українська](getting-started.uk.md) · [Español](getting-started.es.md)

This guide runs Gloxi from source on your own Linux computer. It is for testers and developers: Gloxi 0.1 preview has not been released yet, and there is no package or Flatpak to install. A user guide will follow closer to the release.

What you will have at the end:

1. A local model running on your computer through llama.cpp. Nothing leaves the computer.
2. A chat with Gloxi in the terminal and, optionally, in a GTK window.
3. Optionally, Gloxi working with files in `~/Documents` through Herkos-lite, which asks for your permission and keeps a journal.
4. Optionally, a cloud model with your own API key, with personal data masked before it leaves the computer.

## 1. What you need

- Linux with GNOME. Gloxi is built for openSUSE Aeon first; Fedora, Ubuntu and other distributions with Python 3.11 or newer should work too. The terminal chat works without GNOME.
- 8 GB RAM at minimum, 16 GB recommended. About 3 GB of free disk space for the model.
- A 64-bit Intel or AMD processor; a GPU is not needed.
- `git`, Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/).

**On openSUSE Aeon** the system is immutable, so do all of the following inside a [Distrobox](https://distrobox.it/) container, which Aeon ships with:

```sh
distrobox create --name gloxi-dev
distrobox enter gloxi-dev
```

The container shares your home folder, so files and the model download stay in place between sessions.

Install `uv` if you do not have it:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 2. Get the code

```sh
git clone https://github.com/StDenis1980/VilnaOS.git
cd VilnaOS
```

## 3. Start the local model

Gloxi talks to the model through an OpenAI-compatible API. For development we use `llama-server` from [llama.cpp](https://github.com/ggml-org/llama.cpp) ([ADR 0004](adr/0004-local-model-engine.md)). Get it in one of these ways:

- download a prebuilt archive for Linux from the [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases) page and unpack it;
- `brew install llama.cpp` if you use Homebrew;
- build it from source: `cmake -B build && cmake --build build --config Release -j`, the binary is then `build/bin/llama-server`.

Start the default model, Qwen3 4B in Q4_K_M with "thinking" turned off ([ADR 0008](adr/0008-default-local-model.md)):

```sh
llama-server -hf Qwen/Qwen3-4B-GGUF:Q4_K_M --port 8080 -c 4096 --jinja \
  --chat-template-kwargs '{"enable_thinking":false}'
```

- The first start downloads about 2.5 GB from Hugging Face into `~/.cache/llama.cpp`. Later starts work offline. If you already have the `.gguf` file, pass `-m /path/to/Qwen3-4B-Q4_K_M.gguf` instead of `-hf ...`.
- `--jinja` turns on the chat template that knows about tools; newer builds have it on by default. Gloxi needs it for the files tool.
- Do not use `--reasoning-budget 0` to turn off thinking: in some builds it breaks the answer parsing. Use `enable_thinking` as above.
- Leave this terminal open. The server listens on `http://127.0.0.1:8080/v1`, which is also Gloxi's default address.

For a weaker computer, Qwen2.5 1.5B Instruct (`-hf Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M`, about 1 GB) is faster but noticeably less capable. Any other OpenAI-compatible server works too, for example Ollama: start Gloxi with `--url http://127.0.0.1:11434/v1 --model <model name>`.

## 4. Chat in the terminal

In a second terminal:

```sh
cd VilnaOS/gloxi
uv sync
uv run gloxi ask "What can you do?"
uv run gloxi
```

`uv sync` creates a virtual environment in `gloxi/.venv` with Gloxi, the privacy gateway, Herkos-lite and the files tool server.

`uv run gloxi` opens an interactive chat. Each answer is marked with where it came from, for example `[local]`. Commands:

| Command | What it does |
| --- | --- |
| `/reset` | Start a new conversation |
| `/local <text>`, `/cloud <text>` | Send one message to the local or the cloud model |
| `/exit` | Quit |

## 5. The GTK window (optional)

The window needs PyGObject, GTK 4 and libadwaita from the system, so it runs in a separate virtual environment that can see system packages. Install them first:

```sh
# Fedora
sudo dnf install python3-gobject gtk4 libadwaita
# Ubuntu, Debian
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
# openSUSE Tumbleweed (the default Distrobox image on Aeon)
sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1
```

Then, from the `gloxi/` folder:

```sh
python3 -m venv --system-site-packages .venv-gtk
.venv-gtk/bin/pip install -e ../privacy-gateway -e ../mcp/files -e ../herkos -e ".[tools]"
.venv-gtk/bin/gloxi-gtk
```

The local packages have to be listed explicitly, because pip does not read uv's settings and would look for them on PyPI.

The window title shows whether the cloud is on. Above each answer you can see where it came from: "On this computer" or "Cloud: host, masked". The "+" button starts a new conversation. Interface texts are in English for now; Ukrainian and English translations will come with the Flatpak package.

## 6. Files through Herkos (optional)

Gloxi itself never touches files. It asks Herkos-lite, which checks the risk level, asks you when needed, runs the files tool and writes every action to a journal. By default only `~/Documents` is allowed.

Start Herkos in a third terminal, from the `gloxi/` folder:

```sh
uv run herkos serve --confirm terminal      # asks for permission in this terminal
# or, with the GTK environment from step 5, in a window:
.venv-gtk/bin/herkos serve
```

Then start Gloxi again (`uv run gloxi` or `.venv-gtk/bin/gloxi-gtk`) and ask, for example, "What files are in my Documents folder?" or "Create notes.txt in Documents with a shopping list". Each call appears in the chat, for example `[files: read ~/Documents/plan.txt (done)]`.

Good to know:

- Gloxi 0.1 cannot delete files: deleting is risk level R3, and Herkos-lite carries out only R0 to R2.
- After Gloxi has read or searched files, the conversation contains outside text, so Herkos refuses to change files until you start a new conversation (`/reset` or "+").
- Every change keeps a backup for 30 days. To undo one, find its id in the journal and run `uv run herkos undo <action_id>`.
- Other useful commands: `uv run herkos journal show`, `uv run herkos journal verify`, `uv run herkos permissions list`.
- Allowed folders and other settings are in `~/.config/herkos/config.toml`, for example `allowed_roots = ["~/Documents", "~/Desktop"]`.
- Without Herkos running, Gloxi simply chats. `GLOXI_TOOLS=0` turns the files tool off.

Herkos-lite runs as your own user and does not isolate anything yet; that comes with the system Herkos in the Vilna image.

## 7. A cloud model with your own key (optional)

By default nothing leaves your computer. To let Gloxi use a cloud model for long or complex requests, give it any OpenAI-compatible provider and your own key in `~/.config/gloxi/config.toml`:

```toml
[cloud]
url = "https://api.mistral.ai/v1"
model = "mistral-small-latest"

[router]
cloud_allowed = true
```

and put the key in an environment variable rather than in the file:

```sh
export GLOXI_CLOUD_API_KEY=...
```

With `mode = "auto"`, short messages stay local and messages longer than 1500 characters go to the cloud; `/cloud <text>` sends one message there on purpose. Before anything is sent, the [privacy gateway](../privacy-gateway/README.md) replaces email addresses, phone numbers, IBANs, card and document numbers, keys and passwords with placeholders like `[EMAIL_1]` and puts the real values back into the answer. Names in free text are not recognised yet. The cloud model never gets the files tool. All settings and environment variables are listed in [`gloxi/README.md`](../gloxi/README.md#налаштування).

## 8. Tests and checks

Each component has its own tests, which need no real model:

```sh
cd gloxi            # or herkos, privacy-gateway, mcp/files
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

From the repository root, `python3 tools/check_spdx.py` checks the licence headers. CI runs the same commands on every pull request.

To measure how well a model handles Gloxi's 30 test commands, see [`benchmarks.md`](benchmarks.md).

## 9. Common problems

| Symptom | What to do |
| --- | --- |
| Gloxi says it cannot reach the local model | Check that `llama-server` is running and listening on port 8080: `curl http://127.0.0.1:8080/v1/models`. For another address use `--url` or `GLOXI_LOCAL_URL` |
| The first answer takes a long time | The model is loading or still downloading; watch the `llama-server` terminal. On 8 GB machines try the smaller model from step 3 |
| The model writes long "thinking" before answering | `llama-server` was started without `--chat-template-kwargs '{"enable_thinking":false}'` |
| Gloxi does not use the files tool | Herkos is not running, or `llama-server` was started without `--jinja` (older builds) |
| `herkos: vilna-mcp-files is not installed; no files tool` | Start Herkos from the `gloxi/` folder after `uv sync`, or install `../mcp/files` into the GTK environment as in step 5 |
| `XDG_RUNTIME_DIR is not set` | Herkos needs a desktop session, which sets it. Over ssh, log in to the desktop first, or run without Herkos |
| `gloxi-gtk` fails with `No module named 'gi'` | The GTK environment was created without `--system-site-packages`, or PyGObject is not installed in the system (step 5) |

Found something else? Please open an issue.

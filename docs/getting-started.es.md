# Guía de inicio: compilar y ejecutar Gloxi 0.1 preview

[English](getting-started.md) · [Українська](getting-started.uk.md) · **Español**

Esta guía ejecuta Gloxi desde el código fuente en tu propio ordenador con Linux. Está pensada para personas que prueban el proyecto y para desarrolladores: Gloxi 0.1 preview todavía no se ha publicado y aún no hay paquete ni Flatpak que instalar. La guía de usuario llegará más cerca de la publicación.

Lo que tendrás al final:

1. Un modelo local funcionando en tu ordenador con llama.cpp. Nada sale del ordenador.
2. Un chat con Gloxi en la terminal y, si quieres, en una ventana GTK.
3. Opcionalmente, Gloxi trabajando con archivos de `~/Documents` a través de Herkos-lite, que pide tu permiso y lleva un registro.
4. Opcionalmente, un modelo en la nube con tu propia clave de API; los datos personales se enmascaran antes de salir del ordenador.

## 1. Qué necesitas

- Linux con GNOME. Gloxi se hace primero para openSUSE Aeon; Fedora, Ubuntu y otras distribuciones con Python 3.11 o posterior también deberían funcionar. El chat en terminal funciona sin GNOME.
- Al menos 8 GB de RAM, se recomiendan 16 GB. Unos 3 GB libres en disco para el modelo.
- Un procesador Intel o AMD de 64 bits; no hace falta tarjeta gráfica.
- `git`, Python 3.11 o posterior y [uv](https://docs.astral.sh/uv/).

`gloxi check` (o `flatpak run --command=gloxi io.github.stdenis1980.Gloxi check`) dice si este ordenador basta para el modelo local e imprime un breve resumen del hardware para el formulario de comentarios, sin nada personal.

**En openSUSE Aeon** el sistema es inmutable, así que haz todo lo que sigue dentro de un contenedor [Distrobox](https://distrobox.it/), que viene con Aeon:

```sh
distrobox create --name gloxi-dev
distrobox enter gloxi-dev
```

El contenedor comparte tu carpeta personal, así que los archivos y el modelo descargado se conservan entre sesiones.

Instala `uv` si aún no lo tienes:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 2. Obtener el código

```sh
git clone https://github.com/StDenis1980/VilnaOS.git
cd VilnaOS
```

## 3. Arrancar el modelo local

Gloxi habla con el modelo a través de una API compatible con OpenAI. Para el desarrollo usamos `llama-server` de [llama.cpp](https://github.com/ggml-org/llama.cpp) ([ADR 0004](adr/0004-local-model-engine.md)). Puedes obtenerlo de una de estas formas:

- descargar un archivo precompilado para Linux de la página de [versiones de llama.cpp](https://github.com/ggml-org/llama.cpp/releases) y descomprimirlo;
- `brew install llama.cpp` si usas Homebrew;
- compilarlo desde el código: `cmake -B build && cmake --build build --config Release -j`; el programa queda en `build/bin/llama-server`.

Arranca el modelo por defecto, Qwen3 4B en Q4_K_M con el «razonamiento» desactivado ([ADR 0008](adr/0008-default-local-model.md)):

```sh
llama-server -hf Qwen/Qwen3-4B-GGUF:Q4_K_M --port 8080 -c 4096 --jinja \
  --chat-template-kwargs '{"enable_thinking":false}'
```

- El primer arranque descarga unos 2,5 GB de Hugging Face en `~/.cache/llama.cpp`. Después funciona sin conexión. Si ya tienes el archivo `.gguf`, usa `-m /ruta/a/Qwen3-4B-Q4_K_M.gguf` en lugar de `-hf ...`.
- `--jinja` activa la plantilla de chat que conoce las herramientas; en las versiones recientes viene activada por defecto. Gloxi la necesita para la herramienta de archivos.
- No uses `--reasoning-budget 0` para desactivar el razonamiento: en algunas versiones rompe el análisis de la respuesta. Usa `enable_thinking`, como arriba.
- Deja esta terminal abierta. El servidor escucha en `http://127.0.0.1:8080/v1`, que es también la dirección por defecto de Gloxi.

Para un ordenador más modesto existe Qwen2.5 1.5B Instruct (`-hf Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M`, unos 1 GB): más rápido, pero bastante menos capaz. También sirve cualquier otro servidor compatible con OpenAI, por ejemplo Ollama: arranca Gloxi con `--url http://127.0.0.1:11434/v1 --model <nombre del modelo>`.

## 4. Chat en la terminal

En una segunda terminal:

```sh
cd VilnaOS/gloxi
uv sync
uv run gloxi ask "¿Qué sabes hacer?"
uv run gloxi
```

`uv sync` crea un entorno virtual en `gloxi/.venv` con Gloxi, la pasarela de privacidad, Herkos-lite y el servidor de la herramienta de archivos.

`uv run gloxi` abre un chat interactivo. Cada respuesta indica de dónde viene, por ejemplo `[local]`. Comandos:

| Comando | Qué hace |
| --- | --- |
| `/reset` | Empezar una conversación nueva |
| `/local <texto>`, `/cloud <texto>` | Enviar un mensaje al modelo local o al de la nube |
| `/exit` | Salir |

## 5. La ventana GTK (opcional)

La ventana necesita PyGObject, GTK 4 y libadwaita del sistema, así que funciona en un entorno virtual aparte que puede ver los paquetes del sistema. Instálalos primero:

```sh
# Fedora
sudo dnf install python3-gobject gtk4 libadwaita
# Ubuntu, Debian
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
# openSUSE Tumbleweed (la imagen de Distrobox por defecto en Aeon)
sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1
```

Después, desde la carpeta `gloxi/`:

```sh
python3 -m venv --system-site-packages .venv-gtk
.venv-gtk/bin/pip install -e ../privacy-gateway -e ../mcp/files -e ../herkos -e ".[tools]"
.venv-gtk/bin/gloxi-gtk
```

Los paquetes locales hay que indicarlos explícitamente, porque pip no lee la configuración de uv y los buscaría en PyPI.

El título de la ventana muestra si la nube está activada. Encima de cada respuesta se ve de dónde viene: «On this computer» o «Cloud: host, masked». El botón «+» empieza una conversación nueva. La interfaz sigue el idioma del sistema: por ahora en ucraniano o inglés; el español y otros idiomas llegarán pronto.

## 6. Archivos a través de Herkos (opcional)

Gloxi nunca toca los archivos por sí mismo. Se lo pide a Herkos-lite, que comprueba el nivel de riesgo, te pregunta cuando hace falta, ejecuta la herramienta de archivos y anota cada acción en un registro. Por defecto solo está permitida `~/Documents`.

Arranca Herkos en una tercera terminal, desde la carpeta `gloxi/`:

```sh
uv run herkos serve --confirm terminal      # pide permiso en esta terminal
# o, con el entorno GTK del paso 5, en una ventana:
.venv-gtk/bin/herkos serve
```

Después vuelve a arrancar Gloxi (`uv run gloxi` o `.venv-gtk/bin/gloxi-gtk`) y pídele, por ejemplo, «¿Qué archivos hay en mi carpeta Documents?» o «Crea notes.txt en Documents con una lista de la compra». Cada llamada aparece en el chat, por ejemplo `[files: read ~/Documents/plan.txt (done)]`.

Conviene saber:

- Gloxi 0.1 no puede borrar archivos: borrar es nivel de riesgo R3, y Herkos-lite solo ejecuta de R0 a R2.
- Después de que Gloxi haya leído o buscado archivos, la conversación contiene texto ajeno, así que Herkos no permite modificar archivos hasta que empieces una conversación nueva (`/reset` o «+»).
- Cada cambio guarda una copia de seguridad durante 30 días. Para deshacer uno, busca su id en el registro y ejecuta `uv run herkos undo <action_id>`.
- Otros comandos útiles: `uv run herkos journal show`, `uv run herkos journal verify`, `uv run herkos permissions list`.
- Las carpetas permitidas y otros ajustes están en `~/.config/herkos/config.toml`, por ejemplo `allowed_roots = ["~/Documents", "~/Desktop"]`.
- Si Herkos no está en marcha, Gloxi simplemente conversa. `GLOXI_TOOLS=0` desactiva la herramienta de archivos.

Herkos-lite funciona con tu propio usuario y todavía no aísla nada; el aislamiento llegará con el Herkos del sistema en la imagen de Vilna.

## 7. Un modelo en la nube con tu propia clave (opcional)

Por defecto nada sale de tu ordenador. Para que Gloxi pueda usar un modelo en la nube con peticiones largas o complejas, indica cualquier proveedor compatible con OpenAI en `~/.config/gloxi/config.toml`:

```toml
[cloud]
url = "https://api.mistral.ai/v1"
model = "mistral-small-latest"

[router]
cloud_allowed = true
```

y pon la clave en una variable de entorno, no en el archivo:

```sh
export GLOXI_CLOUD_API_KEY=...
```

Con `mode = "auto"`, los mensajes cortos se quedan en local y los de más de 1500 caracteres van a la nube; `/cloud <texto>` envía allí un mensaje a propósito. Antes de enviar nada, la [pasarela de privacidad](../privacy-gateway/README.md) sustituye correos, teléfonos, IBAN, números de tarjetas y documentos, claves y contraseñas por marcadores como `[EMAIL_1]` y restituye los valores reales en la respuesta. Los nombres en texto libre todavía no se reconocen. Las contraseñas y claves escritas en el chat se ocultan a todos los modelos, también al local, y se muestran como `••••••` si un modelo las repite. El modelo en la nube nunca recibe la herramienta de archivos. Todos los ajustes y variables de entorno están en [`gloxi/README.md`](../gloxi/README.md#налаштування) (en ucraniano).

## 8. Pruebas y comprobaciones

Cada componente tiene sus propias pruebas, que no necesitan un modelo real:

```sh
cd gloxi            # o herkos, privacy-gateway, mcp/files
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Desde la raíz del repositorio, `python3 tools/check_spdx.py` comprueba las cabeceras de licencia. La CI ejecuta los mismos comandos en cada pull request.

Para medir qué tal resuelve un modelo los 30 comandos de prueba de Gloxi, consulta [`benchmarks.md`](benchmarks.md) (en ucraniano).

## 9. Problemas habituales

| Síntoma | Qué hacer |
| --- | --- |
| Gloxi dice que no puede conectar con el modelo local | Comprueba que `llama-server` está en marcha y escucha en el puerto 8080: `curl http://127.0.0.1:8080/v1/models`. Para otra dirección usa `--url` o `GLOXI_LOCAL_URL` |
| La primera respuesta tarda mucho | El modelo se está cargando o aún se descarga; mira la terminal de `llama-server`. Con 8 GB prueba el modelo más pequeño del paso 3 |
| El modelo «razona» largo rato antes de responder | `llama-server` se arrancó sin `--chat-template-kwargs '{"enable_thinking":false}'` |
| Gloxi no usa la herramienta de archivos | Herkos no está en marcha, o `llama-server` de una versión antigua se arrancó sin `--jinja` |
| `herkos: vilna-mcp-files is not installed; no files tool` | Arranca Herkos desde la carpeta `gloxi/` después de `uv sync`, o instala `../mcp/files` en el entorno GTK como en el paso 5 |
| `XDG_RUNTIME_DIR is not set` | Herkos necesita una sesión de escritorio, que define esa variable. Por ssh, inicia antes sesión en el escritorio o trabaja sin Herkos |
| `gloxi-gtk` falla con `No module named 'gi'` | El entorno GTK se creó sin `--system-site-packages`, o PyGObject no está instalado en el sistema (paso 5) |

¿Has encontrado otra cosa? Abre un issue.

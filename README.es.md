<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/design/logo/Vilna_logo_dark-bg.svg">
    <img src="docs/design/logo/Vilna_logo_color.svg" alt="Vilna" width="180">
  </picture>
</p>

# Vilna

[English](README.md) · [Українська](README.uk.md) · **Español**

Vilna es un sistema operativo Linux libre para toda Europa, con un asistente de IA integrado, **Gloxi**, y una capa de seguridad, **Herkos**, por la que tiene que pasar cada acción del asistente.

*Free as in freedom.*

> **Estado: desarrollo temprano.** Todavía no hay ninguna versión publicada. La primera, **Gloxi 0.1 preview** para escritorios Linux con GNOME, está prevista para finales de enero de 2027. Ya se puede compilar y ejecutar desde el código fuente: consulta la [guía de inicio](docs/getting-started.es.md).

## Qué es Vilna

- **Base:** primero openSUSE Aeon (GNOME), más adelante Kalpa (KDE). El sistema es inmutable, las actualizaciones son transaccionales y se pueden revertir con instantáneas de btrfs. La alternativa de reserva es Fedora Atomic (bootc).
- **Gloxi** es el asistente. Funciona como servicio del sistema, trabaja con archivos, aplicaciones y ajustes, lleva tu calendario y te ayuda a organizar el tiempo. Nunca toca el sistema directamente.
- **Herkos** es la capa de seguridad. Cada acción que Gloxi quiere realizar pasa por una única puerta de permisos, que comprueba el nivel de riesgo, te pregunta cuando hace falta y escribe un registro que no se puede alterar sin que se note. Incluye además un filtro contra la inyección de instrucciones (Keros) y la posibilidad de deshacer acciones mediante el registro y las instantáneas (Mitos). Especificación: [`herkos/docs/permissions.md`](herkos/docs/permissions.md).
- **Pasarela de privacidad.** Antes de que algo salga hacia un modelo en la nube, los datos personales (correos, teléfonos, IBAN, números de documentos, claves y contraseñas) se sustituyen en tu dispositivo por marcadores como `[EMAIL_1]`, y los valores reales se restituyen en la respuesta.
- **Herramientas:** servidores MCP para archivos, aplicaciones, ajustes, calendario CalDAV y correo IMAP.
- **Modelos.** Un enrutador envía las peticiones privadas y sencillas a un modelo local pequeño, y las complejas a la nube solo si activas la nube y das tu propia clave. El modelo local por defecto es Qwen3 4B ([ADR 0008](docs/adr/0008-default-local-model.md)); se puede sustituir por cualquier servidor compatible con OpenAI.
- **Hardware mínimo:** Intel Core de 8.ª generación o AMD Ryzen 2000, 8 GB de RAM. Se recomiendan 16 GB para trabajar con comodidad con el modelo local.

## Probar Gloxi 0.1 preview

Por ahora Gloxi funciona como versión de desarrollo en openSUSE Aeon, Fedora, Ubuntu o cualquier otro Linux con Python 3.11 o posterior. En resumen:

```sh
llama-server -hf Qwen/Qwen3-4B-GGUF:Q4_K_M --port 8080 -c 4096 --jinja \
  --chat-template-kwargs '{"enable_thinking":false}'

git clone https://github.com/StDenis1980/VilnaOS.git
cd VilnaOS/gloxi
uv sync
uv run gloxi
```

La guía completa explica cómo instalar llama.cpp y uv, la ventana GTK, el trabajo con archivos a través de Herkos, un modelo en la nube opcional y los problemas habituales: [docs/getting-started.es.md](docs/getting-started.es.md).

## Hoja de ruta

| Cuándo | Qué |
| --- | --- |
| Enero de 2027 | **Gloxi 0.1 preview**: chat con un modelo local, enrutador de modelos, pasarela de privacidad, Herkos-lite con la herramienta de archivos |
| Febrero a marzo de 2027 | Gloxi 0.2: calendario, ajustes de GNOME, panel lateral, repositorio Flatpak de Vilna |
| Abril a septiembre de 2027 | Primera imagen de Vilna sobre openSUSE Aeon, Gloxi como servicio del sistema, Herkos del sistema en Rust |
| Hasta enero de 2028 | Versión alfa de Vilna |
| 2028 | Beta pública |

Las fechas son un plan, no una promesa. Detalles: [`docs/roadmap.md`](docs/roadmap.md) (en ucraniano).

## Estructura del repositorio

Es un monorrepositorio: cada componente vive en su propia carpeta de primer nivel, con su propio `README.md`.

| Carpeta | Contenido |
| --- | --- |
| [`gloxi/`](gloxi/) | El asistente Gloxi: chat en terminal, ventana GTK, enrutador de modelos, cliente de la herramienta de archivos |
| [`herkos/`](herkos/) | Capa de seguridad: puerta de permisos, Keros, Mitos. Herkos-lite (Python) para Gloxi 0.1 |
| [`privacy-gateway/`](privacy-gateway/) | Biblioteca de la pasarela de privacidad: enmascaramiento reversible en el dispositivo (Apache-2.0) |
| [`mcp/`](mcp/) | Servidores MCP de herramientas, empezando por archivos (Apache-2.0) |
| [`sdk/`](sdk/) | SDK para desarrolladores externos (Apache-2.0) |
| [`image/`](image/) | La imagen del sistema Vilna sobre openSUSE Aeon / Kalpa |
| [`shell-extension/`](shell-extension/) | Extensión de GNOME Shell: distribución latina en los campos de contraseña, Gloxi encima de las demás ventanas, una franja de pantalla para el panel de Gloxi |
| [`backup/`](backup/) | Copias de seguridad cifradas de los datos de Vilna (restic, claves en Herkos) |
| [`obs/`](obs/) | Proyecto y paquetes RPM de Vilna en Open Build Service |
| [`docs/`](docs/) | Hoja de ruta, [decisiones de arquitectura](docs/adr/), [mediciones](docs/benchmarks.md), [brief de diseño](docs/design/brief.md) |

Por ahora la mayoría de los documentos del proyecto están en ucraniano. Los puntos de entrada, este README y la guía de inicio, están en inglés, ucraniano y español.

## Contribuir

Todavía no aceptamos contribuciones de código externas: antes hace falta un acuerdo de licencia de contribuidor (CLA). Los informes de errores y las ideas son bienvenidos como issues.

## Licencia

[GPL-3.0-or-later](LICENSE) por defecto: el sistema operativo, Gloxi y Herkos. Los componentes compartidos pensados para usarse fuera de Vilna (pasarela de privacidad, servidores MCP, SDK) son Apache-2.0, cada uno con su propio archivo `LICENSE`. Detalles: [`LICENSING.md`](LICENSING.md).

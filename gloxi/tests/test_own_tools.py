# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings and the system through their own functions, not other apps' buttons.

On the laptop (2026-11-26) "Увімкнути темну тему" and "go back to before the
update" got neither the settings nor the system functions among the eight
Gloxi offers, and the model pressed a made-up 'dark_mode_button' through
AT-SPI. The functions are chosen by the stems of program_words.toml.
"""

import pytest
from test_mail_scenario import offered

from gloxi.bench.mail_scenario import vilna_programs
from gloxi.tools import FileTools

pytest.importorskip("herkos")

DARK = {
    "uk": "Увімкнути темну тему",
    "en": "Turn on dark mode",
    "es": "Activa el modo oscuro",
    "pl": "Włącz ciemny motyw",
    "de": "Dunkles Design einschalten",
    "fr": "Active le thème sombre",
    "it": "Attiva il tema scuro",
    "ro": "Activează tema întunecată",
    "cs": "Zapni tmavý režim",
    "lt": "Įjunk tamsią temą",
    "ca": "Activa el tema fosc",
    "gl": "Activa o tema escuro",
    "eu": "Gai iluna aktibatu",
    "pt": "Ativa o tema escuro",
}


@pytest.mark.parametrize("lang", DARK)
def test_dark_style_brings_the_settings_function_in_every_language(lang):
    assert "settings_set_color_scheme" in offered(vilna_programs(), DARK[lang])


@pytest.mark.parametrize(
    ("text", "function"),
    [
        ("Зроби світлу тему", "settings_set_color_scheme"),
        ("Увімкни нічне світло", "settings_set_night_light"),
        ("Зроби гучніше", "settings_set_volume"),
        ("Увімкни «Не турбувати»", "settings_set_do_not_disturb"),
        ("Поверни систему, як було до оновлення", "system_rollback"),
        ("Roll back the last system update", "system_rollback"),
        ("Vuelve el sistema a como estaba antes de la actualización", "system_rollback"),
        ("Przywróć system sprzed aktualizacji", "system_rollback"),
    ],
)
def test_other_settings_and_the_return_are_offered(text, function):
    assert function in offered(vilna_programs(), text)


def test_the_prompt_sends_settings_to_their_functions_and_atspi_ids_to_list_windows():
    t = FileTools(_connector(vilna_programs()), [])
    prompt = t.prompt()
    assert "settings_*" in prompt and "system_*" in prompt
    assert "never 1, 2, 3 by row" in prompt
    assert "atspi_list_windows" in prompt and "never make an id up" in prompt
    # Without those programs, nothing about them.
    bare = FileTools(_connector([]), []).prompt()
    assert "settings_*" not in bare and "atspi" not in bare


def _connector(listed):
    from test_tools import Connector, FakeHerkos

    return Connector(FakeHerkos(listed=FakeHerkos.LISTED + listed))

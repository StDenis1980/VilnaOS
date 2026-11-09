# SPDX-License-Identifier: Apache-2.0
"""The measurement code must score correctly before anyone pays for a run."""

import re

import pytest
from pg_bench import names, placeholders
from pg_bench.providers import ProviderError, Target

TASKS = placeholders.load_tasks()


def body(message: str) -> str:
    return message.split("\n\n", 1)[1]


def run(chat, fmt="square", prompt="hint", tasks=TASKS):
    return placeholders.run([("fake", chat)], tasks, [fmt], [prompt])


# --- placeholders ------------------------------------------------------------


def test_every_task_has_something_to_hide():
    assert len(TASKS) >= 10
    assert {t.lang for t in TASKS} >= {"uk", "en", "es", "pl", "de", "fr", "it", "ro", "cs", "lt"}
    for task in TASKS:
        assert task.keep in ("all", "some")
        _, message, sent = placeholders.prepare(task, placeholders.FORMATS["square"])
        assert sent, task.id
        assert "@example.com" not in message, task.id


@pytest.mark.parametrize("fmt", list(placeholders.FORMATS))
def test_a_model_that_copies_placeholders_passes(fmt):
    results = run(lambda system, message: body(message), fmt)
    assert all(r.passed for r in results)
    assert all(o.status == "exact" for r in results for o in r.outcomes)


def test_the_hint_names_the_format_in_use():
    seen = []
    run(lambda system, message: seen.append(system) or body(message), "white", "hint", TASKS[:1])
    assert "⟦EMAIL_1⟧" in seen[0]


@pytest.mark.parametrize("fmt", list(placeholders.FORMATS))
def test_a_changed_case_is_restored_but_not_exact(fmt):
    results = run(lambda system, message: body(message).lower(), fmt)
    statuses = {o.status for r in results for o in r.outcomes}
    assert statuses == {"changed"}
    assert all(r.passed for r in results)


def test_a_double_bracket_answer_is_not_exact_for_single_brackets():
    results = run(lambda s, m: re.sub(r"\[([A-Z_]+_\d+)\]", r"[[\1]]", body(m)))
    assert {o.status for r in results for o in r.outcomes} == {"changed"}


def test_a_translated_placeholder_is_broken():
    results = run(lambda s, m: re.sub(r"\[[A-Z_]+_(\d+)\]", r"[ОСОБА_\1]", body(m)))
    assert not any(r.passed for r in results)
    assert {o.status for r in results for o in r.outcomes} == {"broken"}


def test_a_dropped_value_fails_only_where_all_must_stay():
    results = run(lambda s, m: "Done.")
    for r in results:
        assert {o.status for o in r.outcomes} == {"missing"}
        assert r.passed is (r.keep == "some")


def test_provider_errors_are_recorded():
    def fail(system, message):
        raise ProviderError("HTTP 401")

    results = run(fail, tasks=TASKS[:2])
    assert [r.error for r in results] == ["HTTP 401", "HTTP 401"]
    assert not any(r.passed for r in results)
    summary = placeholders.summary(results)
    assert summary[0]["errors"] == 2


def test_report_shows_the_masked_answer_only():
    results = run(lambda s, m: "Done.", tasks=TASKS[:1])
    report = placeholders.markdown(results, {"Моделі": "fake"})
    assert "Done." in report
    assert "example.com" not in report


def test_targets_parse():
    assert Target.parse("anthropic:claude-haiku-4-5") == Target("anthropic", "claude-haiku-4-5")
    assert str(Target.parse("local:qwen3:4b")) == "local:qwen3:4b"
    for bad in ("claude", "unknown:model", "openai:"):
        with pytest.raises(ValueError):
            Target.parse(bad)


# --- names -------------------------------------------------------------------

SENTENCES = names.load_sentences()


def test_ten_sentences_per_language_with_false_alarm_traps():
    by_lang = {}
    for s in SENTENCES:
        by_lang.setdefault(s.lang, []).append(s)
        s.gold()  # every name is in its sentence
    assert set(by_lang) == {"uk", "en", "es", "pl", "de", "fr", "it", "ro", "cs", "lt"}
    for lang, group in by_lang.items():
        assert len(group) == 10, lang
        assert sum(not s.names for s in group) >= 3, lang


def gold_backend(text):
    return next(s.gold() for s in SENTENCES if s.text == text)


def test_a_perfect_model_scores_full_marks():
    rows = names.evaluate(gold_backend, SENTENCES)
    assert all(r.recall == 1 and r.precision == 1 and not r.misses for r in rows.values())


def test_words_found_one_by_one_still_cover_a_name():
    def by_word(text):
        return [
            (m.start(), m.end())
            for g in gold_backend(text)
            for m in re.finditer(r"\S+", text[: g[1]])
            if m.start() >= g[0]
        ]

    rows = names.evaluate(by_word, SENTENCES)
    assert all(r.recall == 1 for r in rows.values())


def test_a_first_name_alone_is_partial_and_a_city_is_a_false_alarm():
    s = names.Sentence("uk", "Олена Коваль поїхала до Києва.", ("Олена Коваль",))
    rows = names.evaluate(lambda text: [(0, 5), (24, 29)], [s])
    row = rows["uk"]
    assert (row.found, row.partial, row.false_alarms) == (0, 1, 1)
    assert row.precision == 0.5


def test_llm_answers_are_parsed_leniently():
    assert names.parse_names('```json\n["Anna Nowak", "Piotr"]\n```') == ["Anna Nowak", "Piotr"]
    assert names.parse_names("Names: [] (none)") == []
    assert names.parse_names("no list here") == []
    assert names.parse_names('["a", 3, null]') == ["a"]
    text = "Anna Nowak i Anna"
    assert names.spans_of(text, ["Anna", "Ewa", ""]) == [(0, 4), (13, 17)]


def test_the_name_model_must_be_local():
    assert names.require_local("http://127.0.0.1:8080/v1")
    with pytest.raises(ValueError):
        names.require_local("https://api.mistral.ai/v1")


# --- keys file ---------------------------------------------------------------


def test_keys_file_sets_only_known_names(tmp_path, monkeypatch):
    from pg_bench.providers import load_keys

    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY", "PATH_EXTRA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "from-shell")
    keys = tmp_path / "keys.env"
    keys.write_text(
        "# comment\nANTHROPIC_API_KEY=sk-ant-fake\nexport MISTRAL_API_KEY='m-fake'\n"
        "OPENAI_API_KEY=from-file\nGEMINI_API_KEY=\nPATH_EXTRA=/evil\n",
        encoding="utf-8",
    )
    keys.chmod(0o600)
    assert load_keys(keys) == ["ANTHROPIC_API_KEY", "MISTRAL_API_KEY"]
    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-fake"
    assert os.environ["MISTRAL_API_KEY"] == "m-fake"
    assert os.environ["OPENAI_API_KEY"] == "from-shell"  # the shell wins
    assert "PATH_EXTRA" not in os.environ


def test_keys_file_others_can_read_is_refused(tmp_path):
    from pg_bench.providers import load_keys

    keys = tmp_path / "keys.env"
    keys.write_text("ANTHROPIC_API_KEY=sk-ant-fake\n", encoding="utf-8")
    keys.chmod(0o644)
    with pytest.raises(ProviderError, match="chmod 600"):
        load_keys(keys)
    assert load_keys(tmp_path / "missing.env") == []


def test_report_names_keep_dots_in_model_names(tmp_path):
    import run

    stem = run._out(str(tmp_path), "names-gliner-urchade_gliner_multi-v2.1")
    assert stem.with_suffix(".md").name.startswith("names-gliner-urchade_gliner_multi-v2.1-")
    assert stem.with_suffix(".md").name.endswith(".md")

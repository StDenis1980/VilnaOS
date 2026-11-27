#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_reproducible.py (sources of non-reproducible builds).

Every rule has snippets that must be found and snippets that must stay quiet;
the last class runs ``main()`` on a small repository made in a temporary
folder.

Run: python3 tools/test_check_reproducible.py
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_reproducible as cr  # noqa: E402  (after sys.path.insert above)


def py(source: str, path: str = "tools/demo.py") -> list[str]:
    """The rules found in a Python snippet."""
    return [p.rule for p in cr.check_text(path, textwrap.dedent(source))]


def spec(body: str, section: str = "%build") -> list[str]:
    """The rules found in a spec with the lines in a script section."""
    text = f"Name: demo\nVersion: 1\n\n{section}\n{textwrap.dedent(body)}\n%files\n/x\n"
    return [p.rule for p in cr.check_text("obs/packages/demo/demo.spec", text)]


def sh(body: str) -> list[str]:
    return [p.rule for p in cr.check_text("obs/images/demo/config.sh", textwrap.dedent(body))]


class TimeRule(unittest.TestCase):
    def test_current_time_is_found(self) -> None:
        for source in (
            "import time\nx = time.time()",
            "import time\nx = time.time_ns()",
            "from time import time\nx = time()",
            "import time as t\nx = t.time()",
            "from datetime import datetime\nx = datetime.now()",
            "import datetime\nx = datetime.datetime.utcnow()",
            "import datetime as dt\nx = dt.datetime.today()",
            "from datetime import date\nx = date.today()",
            "import time\nx = time.localtime()",
            "import time\nx = time.gmtime()",
            "import time\nx = time.strftime('%Y')",
            "from datetime import datetime as D\nx = D.now()",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), ["time"])

    def test_given_time_is_quiet(self) -> None:
        for source in (
            "import time\nx = time.gmtime(0)",
            "import time\nx = time.strftime('%Y', t)",
            "import time\nx = time.monotonic()",
            "import time\nx = time.perf_counter()",
            "from datetime import datetime\nx = datetime.fromtimestamp(5)",
            "from datetime import datetime\nx = datetime(2020, 1, 1)",
            "import time\nx = time.sleep(1)",
            "x = now()",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), [])

    def test_source_date_epoch_makes_it_quiet(self) -> None:
        source = """
            import os, time
            stamp = int(os.environ.get("SOURCE_DATE_EPOCH") or time.time())
        """
        self.assertEqual(py(source), [])

    def test_unrelated_object_named_time_is_not_the_module(self) -> None:
        self.assertEqual(py("class C:\n    def time(self): ...\nC().time()"), [])

    def test_mtime_is_found(self) -> None:
        for source in (
            "import os\nx = os.path.getmtime('a')",
            "import os\nx = os.stat('a').st_mtime",
            "from pathlib import Path\nx = Path('a').stat().st_mtime_ns",
            "import os\nx = os.path.getctime('a')",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), ["mtime"])

    def test_mtime_with_source_date_epoch_is_quiet(self) -> None:
        self.assertEqual(
            py("import os\nos.environ['SOURCE_DATE_EPOCH']\nx = os.stat('a').st_mtime"),
            [],
        )

    def test_host_identity_is_found(self) -> None:
        for source in (
            "import socket\nsocket.gethostname()",
            "import getpass\ngetpass.getuser()",
            "import platform\nplatform.node()",
            "import os\nos.getlogin()",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), ["host"])


class RandomRule(unittest.TestCase):
    def test_shared_generator_is_found(self) -> None:
        for source in (
            "import random\nrandom.random()",
            "import random\nrandom.choice([1, 2])",
            "import random\nrandom.shuffle(x)",
            "from random import randint\nrandint(1, 2)",
            "import numpy as np\nnp.random.rand(3)",
            "import numpy\nnumpy.random.normal(size=3)",
            "from numpy import random\nrandom.randn(3)",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), ["random"])

    def test_seeded_use_is_quiet(self) -> None:
        for source in (
            "import random\nrandom.seed(7)\nrandom.random()",
            "import random\nr = random.Random(7)\nr.random()",
            "import numpy as np\nrng = np.random.default_rng(7)\nrng.normal()",
            "import numpy as np\nnp.random.seed(1)\nnp.random.rand(2)",
            "import numpy as np\nrng = np.random.default_rng(sum(map(ord, 'a')))",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), [])

    def test_generator_without_seed_is_found(self) -> None:
        for source in (
            "import random\nrandom.seed()",
            "import random\nrandom.Random()",
            "import numpy as np\nnp.random.default_rng()",
            "import numpy as np\nnp.random.RandomState()",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), ["random"])

    def test_system_entropy_is_found(self) -> None:
        for source in (
            "import os\nos.urandom(8)",
            "import uuid\nuuid.uuid4()",
            "import uuid\nuuid.uuid1()",
            "import secrets\nsecrets.token_hex(8)",
            "import random\nrandom.SystemRandom()",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), ["random"])

    def test_uuid_from_a_name_is_quiet(self) -> None:
        self.assertEqual(py("import uuid\nuuid.uuid5(uuid.NAMESPACE_DNS, 'a')"), [])

    def test_text_hash_is_found(self) -> None:
        self.assertEqual(py("x = hash('a' + name)"), ["random"])
        self.assertEqual(py("import numpy as np\nnp.random.default_rng(hash(seed))"), ["random"])

    def test_hash_of_a_number_and_hashlib_are_quiet(self) -> None:
        self.assertEqual(py("x = hash(5)"), [])
        self.assertEqual(py("import hashlib\nhashlib.sha256(b'a').hexdigest()"), [])

    def test_method_of_a_generator_is_quiet(self) -> None:
        self.assertEqual(py("def f(rng):\n    return rng.random() + rng.integers(3)"), [])


class OrderRule(unittest.TestCase):
    def test_unsorted_listing_is_found(self) -> None:
        for source in (
            "import os\nfor f in os.listdir('.'):\n    pass",
            "import os\nx = os.listdir('.')",
            "import glob\nx = glob.glob('*.c')",
            "import glob\nx = list(glob.iglob('*.c'))",
            "from glob import glob\nx = glob('*.c')",
            "from pathlib import Path\nfor p in Path('.').glob('*.c'):\n    pass",
            "from pathlib import Path\nx = list(Path('.').rglob('*'))",
            "from pathlib import Path\nfor p in Path('.').iterdir():\n    pass",
            "import os\nx = list(os.scandir('.'))",
            "import os\nfor r, d, f in os.walk('.'):\n    pass",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), ["order"])

    def test_sorted_listing_is_quiet(self) -> None:
        for source in (
            "import os\nx = sorted(os.listdir('.'))",
            "import glob\nx = sorted(glob.glob('*.c'))",
            "from pathlib import Path\nx = sorted(Path('.').glob('*'), key=str)",
            "from pathlib import Path\nx = sorted(p.name for p in Path('.').iterdir())",
            "from pathlib import Path\nx = len(list(Path('.').iterdir()))",
            "import os\nx = set(os.listdir('.'))",
            "import os\nx = os.listdir('.')\nx.sort()",
            "import os\nx = os.listdir('.')\nfor y in sorted(x):\n    pass",
            "import os\nx = any(os.listdir('.'))",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), [])

    def test_walk_with_sorted_names_is_quiet(self) -> None:
        source = """
            import os
            for root, dirs, files in os.walk('.'):
                dirs.sort()
                for f in sorted(files):
                    pass
        """
        self.assertEqual(py(source), [])

    def test_walk_with_only_files_sorted_is_found(self) -> None:
        source = """
            import os
            for root, dirs, files in os.walk('.'):
                for f in sorted(files):
                    pass
        """
        self.assertEqual(py(source), ["order"])

    def test_walk_ignoring_dirs_with_underscore_is_quiet(self) -> None:
        source = """
            import os
            for root, _dirs, files in os.walk('.'):
                files.sort()
        """
        self.assertEqual(py(source), [])

    def test_sorting_in_another_function_does_not_count(self) -> None:
        source = """
            import os
            def a():
                x = os.listdir('.')
                return x
            def b(x):
                x.sort()
        """
        self.assertEqual(py(source), ["order"])

    def test_set_loop_is_found(self) -> None:
        for source in (
            "for x in {1, 2}:\n    pass",
            "for x in set(a):\n    pass",
            "s = set()\nfor x in s:\n    pass",
            "s = {1}\nx = list(s)",
            "s = {1}\nx = ','.join(s)",
            "s = {1}\nx = [y for y in s]",
            "x = tuple(frozenset(a))",
            "a = {1}\nb = {2}\nfor x in a | b:\n    pass",
            "s: set[int] = set()\nfor x in s:\n    pass",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), ["order"])

    def test_sorted_set_and_membership_are_quiet(self) -> None:
        for source in (
            "s = {1}\nfor x in sorted(s):\n    pass",
            "s = {1}\nx = 1 in s",
            "s = {1}\ns.add(2)\nx = len(s)",
            "s = {1}\nx = sorted(s)",
            "s = {1}\ns = [3, 4]\nfor x in s:\n    pass",
            "for x in [1, 2]:\n    pass",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), [])


class ArchiveRule(unittest.TestCase):
    def test_tar_add_without_filter_is_found(self) -> None:
        source = """
            import tarfile
            with tarfile.open('a.tar', 'w') as tar:
                tar.add('x')
        """
        self.assertEqual(py(source), ["archive"])

    def test_tar_add_with_filter_and_addfile_are_quiet(self) -> None:
        source = """
            import tarfile
            with tarfile.open('a.tar', 'w') as tar:
                tar.add('x', filter=clean)
                tar.addfile(info, data)
        """
        self.assertEqual(py(source), [])

    def test_set_add_is_not_tar_add(self) -> None:
        self.assertEqual(py("s = set()\ns.add(1)"), [])

    def test_zip_write_is_found_and_writestr_is_quiet(self) -> None:
        source = "import zipfile\nz = zipfile.ZipFile('a.zip', 'w')\nz.write('x')"
        self.assertEqual(py(source), ["archive"])
        source = "import zipfile\nz = zipfile.ZipFile('a.zip', 'w')\nz.writestr(info, b'')"
        self.assertEqual(py(source), [])

    def test_make_archive_is_found(self) -> None:
        self.assertEqual(py("import shutil\nshutil.make_archive('a', 'gztar', 'd')"), ["archive"])

    def test_gzip_time_is_found(self) -> None:
        for source in (
            "import gzip\ngzip.compress(b'a')",
            "import gzip\ngzip.GzipFile(fileobj=f, mode='wb')",
            "import gzip\ngzip.GzipFile('a.gz', 'w')",
            "import gzip\ngzip.open('a.gz', 'wb')",
            "import tarfile\ntarfile.open('a.tgz', 'w:gz')",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), ["archive"])

    def test_gzip_with_zero_time_and_reading_are_quiet(self) -> None:
        for source in (
            "import gzip\ngzip.compress(b'a', mtime=0)",
            "import gzip\ngzip.GzipFile(fileobj=f, mode='wb', mtime=0)",
            "import gzip\ngzip.open('a.gz')",
            "import gzip\ngzip.open('a.gz', 'rb')",
            "import gzip\ngzip.GzipFile('a.gz', 'rb')",
            "import tarfile\ntarfile.open('a.tgz', 'r:gz')",
            "import tarfile\ntarfile.open(fileobj=buf, mode='w')",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), [])


class PathRule(unittest.TestCase):
    def test_literal_build_paths_are_found(self) -> None:
        for literal in (
            "/home/abuild/rpmbuild",
            "/home/denis/x",
            "/root/build",
            "/Users/denis/x",
            "/builddir/build",
            "/usr/src/packages/BUILD",
            "C:\\\\Users\\\\denis",
        ):
            with self.subTest(literal=literal):
                self.assertEqual(py(f"x = '{literal}'"), ["path"])

    def test_other_literals_are_quiet(self) -> None:
        for literal in (
            "/usr/lib",
            "/etc/home/x",
            "%{buildroot}/root",
            "/rooted",
            "/var/home",
        ):
            with self.subTest(literal=literal):
                self.assertEqual(py(f"x = '{literal}'"), [])

    def test_docstring_may_name_a_path(self) -> None:
        self.assertEqual(py('"""Example: /home/denis/x."""\nx = 1'), [])
        self.assertEqual(py('def f():\n    """Under /root/x."""\n'), [])

    def test_path_written_to_a_file_is_found(self) -> None:
        for source in (
            "from pathlib import Path\nROOT = Path(__file__).resolve().parent\n"
            "out.write_text(str(ROOT))",
            "from pathlib import Path\nROOT = Path(__file__).resolve().parent\n"
            "out.write_text(f'root={ROOT}')",
            "import os\nd = os.getcwd()\nout.write('a' + d)",
            "import os\nd = os.getcwd()\nout.write('%s' % d)",
            "from pathlib import Path\nh = Path.home()\nprint(f'{h}/x')",
            "import tempfile\nd = tempfile.mkdtemp()\nout.write_text('{}'.format(d))",
            "import os\nd = os.path.abspath('.')\nline = f'at {d}'\nout.write_text(line)",
            "from pathlib import Path\nHERE = Path(__file__)\nsub = HERE / 'x'\n"
            "out.write_text(sub.as_posix())",
            "from pathlib import Path\nout.write_text(str(Path('a').resolve()))",
            "import json\nimport os\njson.dump({'cwd': f'{os.getcwd()}'}, fp)",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), ["path"])

    def test_relative_path_written_is_quiet(self) -> None:
        for source in (
            "from pathlib import Path\nROOT = Path(__file__).resolve().parent\n"
            "p = ROOT / 'a'\nout.write_text(str(p.relative_to(ROOT)))",
            "from pathlib import Path\nROOT = Path(__file__).resolve().parent\n"
            "out.write_text(ROOT.name)",
            "from pathlib import Path\nROOT = Path(__file__).resolve().parent\n"
            "p = ROOT / 'a'\nout.write_text(p.name)",
            "import os\nout.write_text(os.path.basename(os.getcwd()))",
            "import os\nout.write_text(os.path.relpath('a', os.getcwd()))",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), [])

    def test_name_of_a_path_is_quiet_even_in_text(self) -> None:
        for source in (
            "from pathlib import Path\nROOT = Path(__file__).resolve()\n"
            "out.write_text(f'built {ROOT.name}')",
            "from pathlib import Path\nROOT = Path(__file__).resolve()\n"
            "out.write_text(str(ROOT.stem) + ROOT.suffix)",
            "from pathlib import Path\nROOT = Path(__file__).resolve()\n"
            "out.write_text(f'{ROOT.parent.name}')",
        ):
            with self.subTest(source=source):
                self.assertEqual(py(source), [])

    def test_result_of_own_function_is_not_a_path(self) -> None:
        source = """
            from pathlib import Path
            ROOT = Path(__file__).resolve().parent
            def count(root):
                return [p.name for p in root.iterdir()]
            n = count(ROOT)
            out.write_text(f"{n} files")
        """
        self.assertEqual(py(source), ["order"])

    def test_path_used_only_to_find_files_is_quiet(self) -> None:
        source = """
            from pathlib import Path
            ROOT = Path(__file__).resolve().parent
            SRC = ROOT / "src"
            def build(page):
                text = (SRC / page).read_text()
                out.write_text(text)
                run(["tool", str(SRC / page)])
        """
        self.assertEqual(py(source), [])

    def test_parameter_hides_a_tainted_module_name(self) -> None:
        source = """
            from pathlib import Path
            ROOT = Path(__file__).resolve().parent
            def f(ROOT):
                out.write_text(str(ROOT))
        """
        self.assertEqual(py(source), [])

    def test_print_to_stderr_is_quiet(self) -> None:
        source = "import os, sys\nd = os.getcwd()\nprint(f'{d}', file=sys.stderr)\nprint(f'{d}')\n"
        self.assertEqual(py(source), ["path"])


class MarkerAndLegacy(unittest.TestCase):
    def test_marker_on_the_line_or_above(self) -> None:
        self.assertEqual(py("import time\nx = time.time()  # repro: ok - log only"), [])
        self.assertEqual(py("import time\n# repro: ok - log only\nx = time.time()"), [])
        self.assertEqual(py("import time\n# repro: ok\n\nx = time.time()"), ["time"])

    def test_marker_in_spec(self) -> None:
        self.assertEqual(spec("date > x  # repro: ok"), [])
        self.assertEqual(spec("# repro: ok\ndate > x"), [])

    def test_legacy_makes_a_warning(self) -> None:
        text = "import time\nx = time.time()\n"
        key = ("tools/demo.py", "time", "x = time.time()")
        old = cr.LEGACY
        try:
            cr.LEGACY = frozenset({key})
            found = cr.check_text("tools/demo.py", text)
            self.assertEqual([p.warning for p in found], [True])
            self.assertIn("warning:", str(found[0]))
            other = cr.check_text("tools/demo.py", text + "y = time.time_ns()\n")
            self.assertEqual([p.warning for p in other], [True, False])
        finally:
            cr.LEGACY = old

    def test_legacy_follows_the_text_not_the_line_number(self) -> None:
        old = cr.LEGACY
        try:
            cr.LEGACY = frozenset({("tools/demo.py", "time", "x = time.time()")})
            found = cr.check_text("tools/demo.py", "import time\n\n\n\nx = time.time()\n")
            self.assertEqual([p.warning for p in found], [True])
        finally:
            cr.LEGACY = old

    def test_syntax_error_is_reported(self) -> None:
        found = cr.check_text("tools/demo.py", "def (:\n")
        self.assertEqual([p.rule for p in found], ["syntax"])

    def test_message_form(self) -> None:
        found = cr.check_text("tools/demo.py", "import time\nx = time.time()\n")
        self.assertTrue(str(found[0]).startswith("tools/demo.py: line 2: "))


class SpecTimeRule(unittest.TestCase):
    def test_date_in_macro_is_found(self) -> None:
        for line in (
            "%define build_date %(date +%Y%m%d)",
            "%global stamp %(date)",
            "%define host %(hostname)",
            "%define who %(whoami)",
            "%define k %(uname -r)",
            "%define rev %(git rev-parse HEAD)",
            "%define x %(echo $(date))",
            "%define x %(id -un)",
            "%define n %(/bin/date)",
        ):
            with self.subTest(line=line):
                text = f"Name: demo\n{line}\n"
                found = [p.rule for p in cr.check_text("obs/packages/demo/demo.spec", text)]
                self.assertEqual(found, ["spec-time"])

    def test_macro_in_comment_and_changelog_is_found(self) -> None:
        for where in ("# built %(date)\nName: demo\n", "%changelog\n* Mon %(date) x\n"):
            with self.subTest(where=where):
                found = [p.rule for p in cr.check_text("obs/packages/demo/demo.spec", where)]
                self.assertEqual(found, ["spec-time"])

    def test_pure_macros_are_quiet(self) -> None:
        for line in (
            "%define v %(echo %{version} | sed 's/~/-/')",
            "%define d %(date -u -d @%{getenv:SOURCE_DATE_EPOCH} +%Y)",
            "%define p %{python3_sitelib}",
            "%define ident %(identity)",
            "%define y %(echo candidate)",
            "%define y %(echo a.b-c)",
        ):
            with self.subTest(line=line):
                text = f"Name: demo\n{line}\n"
                self.assertEqual(cr.check_text("obs/packages/demo/demo.spec", text), [])

    def test_lua_clock_is_found(self) -> None:
        for line in (
            "%define t %{lua: print(os.date('%Y'))}",
            "%define t %{lua: print(os.time())}",
            "%define t %{lua: print(math.random(9))}",
            "%define t %{lua: print(os.getenv('USER'))}",
        ):
            with self.subTest(line=line):
                text = f"Name: demo\n{line}\n"
                found = [p.rule for p in cr.check_text("obs/packages/demo/demo.spec", text)]
                self.assertEqual(found, ["spec-time"])

    def test_lua_without_clock_is_quiet(self) -> None:
        text = "%define t %{lua: print(string.upper('a'))}\n"
        self.assertEqual(cr.check_text("obs/packages/demo/demo.spec", text), [])

    def test_nested_parentheses_are_followed(self) -> None:
        text = "%define t %(echo $(echo (a)) $(date))\n"
        found = [p.rule for p in cr.check_text("obs/packages/demo/demo.spec", text)]
        self.assertEqual(found, ["spec-time"])


class ShellTimeRule(unittest.TestCase):
    def test_commands_are_found(self) -> None:
        for body in (
            "date > stamp",
            "x=$(date +%s)",
            "x=`date`",
            "hostname > h",
            "echo $(whoami)",
            "cd x && date",
            "if true; then uname -r; fi",
            "x=$(id -u)",
            "uuidgen > u",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), ["shell-time"])

    def test_variables_are_found(self) -> None:
        for body in (
            "echo $USER > x",
            "echo ${HOSTNAME} > x",
            "n=$RANDOM",
            "head -c8 /dev/urandom > x",
            "echo $EPOCHSECONDS",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), ["shell-time"])

    def test_date_from_source_date_epoch_is_quiet(self) -> None:
        self.assertEqual(spec('date -u -d "@$SOURCE_DATE_EPOCH" +%Y'), [])
        self.assertEqual(spec("date -u -d @1700000000 +%Y"), [])
        self.assertEqual(spec("date --date=@123"), [])

    def test_words_that_are_not_commands_are_quiet(self) -> None:
        for body in (
            "echo the date and the hostname > x",
            "install -D date %{buildroot}/x",
            "sed -i 's/id/ID/' f",
            "mkdir -p dir/date",
            "echo $USERNAME_FILE",
            "# date",
            "grep -r hostname.conf .",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), [])

    def test_only_script_sections_are_scanned(self) -> None:
        text = "Name: demo\nSummary: date and hostname\n%description\nrun date now\n%files\n/x\n"
        self.assertEqual(cr.check_text("obs/packages/demo/demo.spec", text), [])

    def test_every_script_section_is_scanned(self) -> None:
        for section in (
            "%prep",
            "%build",
            "%install",
            "%check",
            "%post",
            "%pre",
            "%posttrans",
        ):
            with self.subTest(section=section):
                self.assertEqual(spec("date > x", section), ["shell-time"])

    def test_section_ends_at_files(self) -> None:
        text = "%install\nx\n%files\ndate\n"
        self.assertEqual(cr.check_text("obs/packages/demo/demo.spec", text), [])

    def test_continued_line_is_one_command(self) -> None:
        self.assertEqual(spec("echo a \\\n  b $(date)"), ["shell-time"])

    def test_heredoc_expansion(self) -> None:
        self.assertEqual(spec("cat > f <<EOF\nbuilt $(date)\nEOF"), ["shell-time"])
        self.assertEqual(spec("cat > f <<'EOF'\nbuilt $(date)\nEOF"), [])
        self.assertEqual(spec("cat > f <<EOF\nuser $USER\nEOF"), ["shell-time"])

    def test_shell_script_file(self) -> None:
        self.assertEqual(sh("#!/bin/bash\ndate > /etc/built\n"), ["shell-time"])
        self.assertEqual(sh("#!/bin/bash\n# date is not run\necho ok\n"), [])


class BuildPathRule(unittest.TestCase):
    def test_heredoc_with_build_folder_is_found(self) -> None:
        for var in (
            "%{buildroot}",
            "%{_builddir}",
            "$RPM_BUILD_ROOT",
            "${RPM_BUILD_ROOT}",
            "$PWD",
        ):
            for tag in ("EOF", "'EOF'"):
                with self.subTest(var=var, tag=tag):
                    body = f"cat > %{{buildroot}}/f <<{tag}\nroot={var}/x\nEOF"
                    self.assertEqual(spec(body, "%install"), ["build-path"])

    def test_heredoc_target_is_not_content(self) -> None:
        body = "cat > %{buildroot}/etc/f <<'EOF'\nplain\nEOF"
        self.assertEqual(spec(body, "%install"), [])

    def test_echo_and_printf_content_is_found(self) -> None:
        for body in (
            "echo %{buildroot}/x > list",
            "echo 'root: %{_builddir}' >> f",
            'printf "%s\\n" "$PWD" > f',
            "echo $(pwd) > f",
            "echo `pwd` | tee f",
            "echo %{buildroot} \\\n  > f",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body, "%install"), ["build-path"])

    def test_echo_into_the_build_folder_is_quiet(self) -> None:
        for body in (
            "echo hello > %{buildroot}/etc/f",
            "echo 'x' >> %{buildroot}%{_sysconfdir}/f",
            "echo done",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body, "%install"), [])

    def test_sed_replacement_is_found(self) -> None:
        for body in (
            "sed -i 's|@ROOT@|%{buildroot}|' f",
            'sed -i "s#@ROOT@#%{_builddir}/x#g" f',
            "sed 's/@D@/$PWD/' f > g",
            "sed -e 's,@R@,$RPM_BUILD_ROOT,' f",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body, "%install"), ["build-path"])

    def test_sed_pattern_and_target_are_quiet(self) -> None:
        for body in (
            "sed -i 's|%{buildroot}||' f",
            "sed -i 's|@V@|%{version}|' %{buildroot}/f",
            "sed -i 's|/opt/x|%{appdir}|' %{buildroot}/f",
            "sed -n 's/^a = \\(.*\\)$/\\1/p' f",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body, "%install"), [])

    def test_install_options_are_found(self) -> None:
        for body in (
            "./configure --prefix=%{buildroot}/usr",
            "cmake -DCMAKE_INSTALL_PREFIX=%{buildroot}/usr .",
            "./configure --sysconfdir=$RPM_BUILD_ROOT/etc",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), ["build-path"])

    def test_destdir_is_quiet(self) -> None:
        for body in (
            "make install DESTDIR=%{buildroot}",
            "./configure --prefix=%{_prefix}",
            "install -D -m 0644 a %{buildroot}%{_datadir}/a",
            "cp -a tree/. %{buildroot}/",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), [])


class AbsPathRuleInScripts(unittest.TestCase):
    def test_literal_builder_paths_are_found(self) -> None:
        for body in (
            "cp /home/abuild/rpmbuild/BUILD/x y",
            "echo /root/x > f",
            "ls /usr/src/packages/BUILD",
            "cd /builddir/build",
        ):
            with self.subTest(body=body):
                self.assertIn("abs-path", spec(body))

    def test_other_paths_are_quiet(self) -> None:
        for body in (
            "install -d %{buildroot}/root",
            "cp x %{buildroot}%{_prefix}/home/x",
            "chmod 0700 %{buildroot}%{_sysconfdir}/f",
            "ls /usr/lib",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), [])


class ArchiveRuleInScripts(unittest.TestCase):
    GOOD = "tar --sort=name --mtime=@$SOURCE_DATE_EPOCH --owner=0 --group=0 -cf a.tar d"

    def test_tar_without_options_is_found(self) -> None:
        for body in (
            "tar czf a.tgz d",
            "tar -czf a.tgz d",
            "tar --create -f a.tar d",
            "tar -c -f a.tar d",
            "tar cf - d | xz > a.tar.xz",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), ["archive"])

    def test_tar_needs_each_option(self) -> None:
        for body, missing in (
            ("tar --mtime=@0 --owner=0 -cf a.tar d", "--sort=name"),
            ("tar --sort=name --owner=0 -cf a.tar d", "--mtime="),
            ("tar --sort=name --mtime=@0 -cf a.tar d", "--owner=0"),
        ):
            with self.subTest(body=body):
                text = f"%build\n{body}\n"
                found = cr.check_text("obs/packages/demo/demo.spec", text)
                self.assertEqual([p.rule for p in found], ["archive"])
                self.assertIn(missing, found[0].message)

    def test_tar_message_names_what_is_missing(self) -> None:
        text = "%build\ntar --sort=name -cf a.tar d\n"
        found = cr.check_text("obs/packages/demo/demo.spec", text)
        self.assertIn("--mtime=", found[0].message)
        self.assertNotIn("--sort=name,", found[0].message)

    def test_full_tar_and_extraction_are_quiet(self) -> None:
        for body in (
            self.GOOD,
            "tar --sort=name --mtime=@0 --numeric-owner -cf a.tar d",
            "tar xf a.tar",
            "tar -xzf a.tgz -C x",
            "tar tf a.tar",
            "tar -xvf a.tar",
            "tar --extract -f a.tar",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), [])

    def test_gzip_without_n_is_found(self) -> None:
        for body in ("gzip a", "gzip -9 a", "gzip -c a > a.gz", "cat a | gzip > a.gz"):
            with self.subTest(body=body):
                self.assertEqual(spec(body), ["archive"])

    def test_gzip_with_n_and_decompression_are_quiet(self) -> None:
        for body in (
            "gzip -n a",
            "gzip -9n a",
            "gzip --no-name a",
            "gzip -d a.gz",
            "gzip -dc a.gz",
            "gzip -t a.gz",
            "gzip -l a.gz",
            "gunzip a.gz",
            "zcat a.gz",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), [])

    def test_zip(self) -> None:
        self.assertEqual(spec("zip -r a.zip d"), ["archive"])
        self.assertEqual(spec("zip -rX a.zip d"), [])
        self.assertEqual(spec("zip -X -r a.zip d"), [])

    def test_words_like_tar_in_names_are_quiet(self) -> None:
        for body in (
            "install -D guitar %{buildroot}/x",
            "cp tar.conf x",
            "echo tarball > f",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), [])


class OrderRuleInScripts(unittest.TestCase):
    def test_unsorted_listing_is_found(self) -> None:
        for body in (
            "for f in $(ls dir); do echo $f; done",
            "for f in $(find dir -type f); do echo $f; done",
            "for f in `ls`; do echo $f; done",
            "find . -type f -printf '%P\\n' > list",
            "find . -type l | while read -r l; do echo $l; done",
            "find . -type f -print0 | xargs -0 cat > all",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), ["order"])

    def test_sorted_or_acting_find_is_quiet(self) -> None:
        for body in (
            "for f in $(find dir -type f | sort); do echo $f; done",
            "find . -type f -printf '%P\\n' | sort > list",
            "find . -type f -exec chmod 0644 {} +",
            "find %{buildroot} -type f -perm /6000 -exec chmod ug-s {} +",
            "find . -name '*.pyc' -delete",
            "find . -type f -exec chmod 0644 {} \\;",
            "for f in dir/*; do echo $f; done",
            "ls dir > /dev/null || true",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), [])


class CompilerRuleInScripts(unittest.TestCase):
    def test_cargo_without_remap_is_found(self) -> None:
        for body in (
            "cargo build --release --offline",
            "cd rs && cargo install --path .",
            "cargo rustc --release",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), ["rust-path"])

    def test_cargo_with_remap_is_quiet(self) -> None:
        for body in (
            "RUSTFLAGS='--remap-path-prefix=%{_builddir}=.' cargo build --release",
            "export RUSTFLAGS='--remap-path-prefix=%{_builddir}=.'\ncargo build --release",
            "cargo test --release",
            "cargo vendor",
        ):
            with self.subTest(body=body):
                self.assertEqual(spec(body), [])

    def test_go(self) -> None:
        self.assertEqual(spec("go build ./..."), ["go-path"])
        self.assertEqual(spec("go build -trimpath ./..."), [])
        self.assertEqual(spec("go test ./..."), [])


class RepositoryRun(unittest.TestCase):
    """main() on a small git repository in a temporary folder."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.old_root = cr.ROOT
        cr.ROOT = self.root
        self.addCleanup(setattr, cr, "ROOT", self.old_root)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

    def add(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", rel], cwd=self.root, check=True)

    def run_main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cr.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def good_repo(self) -> None:
        self.add(
            "obs/packages/demo/demo.spec",
            "Name: demo\n%install\ninstall -D a b\n%files\n/b\n",
        )
        self.add("tools/build_site.py", "import sys\nsys.exit(0)\n")
        self.add("sounds/make_sounds.py", "x = 1\n")

    def test_good_repository(self) -> None:
        self.good_repo()
        code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "3 build files checked: 0 errors, 0 warnings\n")

    def test_default_files_are_specs_image_scripts_and_build_scripts(self) -> None:
        self.good_repo()
        self.add("obs/images/Demo/config.sh", "true\n")
        self.add("obs/images/Demo/other.sh", "date\n")
        self.add("tools/other.py", "import time\ntime.time()\n")
        self.add("obs/tools/obs_sync.py", "x = 1\n")
        self.add("image/plymouth/make_theme.py", "x = 1\n")
        self.assertEqual(
            cr.default_files(cr.tracked_files(self.root)),
            [
                "image/plymouth/make_theme.py",
                "obs/images/Demo/config.sh",
                "obs/packages/demo/demo.spec",
                "obs/tools/obs_sync.py",
                "sounds/make_sounds.py",
                "tools/build_site.py",
            ],
        )
        self.assertEqual(self.run_main()[0], 0)

    def test_error_gives_exit_1_and_message(self) -> None:
        self.good_repo()
        self.add("sounds/make_sounds.py", "import time\nx = time.time()\n")
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("sounds/make_sounds.py: line 2: ", err)
        self.assertIn("[time]", err)
        self.assertEqual(out, "3 build files checked: 1 errors, 0 warnings\n")

    def test_spec_error(self) -> None:
        self.add("obs/packages/demo/demo.spec", "Name: demo\n%build\ndate > x\n%files\n/x\n")
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("obs/packages/demo/demo.spec: line 3: ", err)

    def test_file_argument(self) -> None:
        self.add("tools/other.py", "import time\ntime.time()\n")
        code, out, err = self.run_main("tools/other.py")
        self.assertEqual(code, 1)
        self.assertIn("tools/other.py: line 2", err)
        self.assertEqual(out, "1 build files checked: 1 errors, 0 warnings\n")

    def test_absolute_file_argument(self) -> None:
        self.add("tools/other.py", "x = 1\n")
        self.assertEqual(self.run_main(str(self.root / "tools" / "other.py"))[0], 0)

    def test_file_outside_the_repository_is_exit_2(self) -> None:
        code, _, err = self.run_main("/etc/hostname")
        self.assertEqual(code, 2)
        self.assertIn("not inside the repository", err)

    def test_missing_file_is_exit_2(self) -> None:
        code, _, err = self.run_main("tools/none.py")
        self.assertEqual(code, 2)
        self.assertIn("tools/none.py: cannot read", err)

    def test_wrong_extension_is_exit_2(self) -> None:
        self.add("README.md", "text\n")
        code, _, err = self.run_main("README.md")
        self.assertEqual(code, 2)
        self.assertIn("not a .py, .spec or .sh file", err)

    def test_unknown_option_is_exit_2(self) -> None:
        self.assertEqual(self.run_main("--bogus")[0], 2)

    def test_help_is_exit_0(self) -> None:
        code, _, err = self.run_main("--help")
        self.assertEqual(code, 0)
        self.assertIn("usage:", err)

    def test_no_build_files_is_exit_2(self) -> None:
        self.add("README.md", "text\n")
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("no build files found", err)

    def test_not_a_git_repository_is_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            cr.ROOT = Path(other)
            code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("git ls-files", err)

    def test_checker_does_not_write_anything(self) -> None:
        self.good_repo()
        before = sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if ".git" not in p.parts
        )
        self.run_main()
        after = sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if ".git" not in p.parts
        )
        self.assertEqual(before, after)


class OwnRepository(unittest.TestCase):
    """The real repo: no errors, and the legacy entries still match something."""

    def test_no_errors_on_the_repository(self) -> None:
        files = cr.default_files(cr.tracked_files(ROOT))
        problems, errors = cr.check_files(ROOT, files)
        self.assertEqual(errors, [])
        self.assertEqual([str(p) for p in problems if not p.warning], [])

    def test_every_legacy_entry_is_still_needed(self) -> None:
        files = cr.default_files(cr.tracked_files(ROOT))
        problems, _ = cr.check_files(ROOT, files)
        seen = {(p.path, p.rule, p.text) for p in problems if p.warning}
        self.assertEqual(sorted(cr.LEGACY - seen), [])

    def test_the_tool_passes_its_own_rules(self) -> None:
        problems, errors = cr.check_files(ROOT, ["tools/check_reproducible.py"])
        self.assertEqual(errors, [])
        self.assertEqual([str(p) for p in problems], [])


if __name__ == "__main__":
    unittest.main()

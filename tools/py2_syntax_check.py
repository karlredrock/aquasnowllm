#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate: AquaSnowLLM must stay valid Python 2.7 and free of Python 3 syntax.

What it checks
--------------
1. If a ``python2.7`` executable is available (``PATH``, ``/usr/bin``,
   ``/opt/local/bin``, ``/usr/local/bin``), the sources are compiled with it.
   That is the authoritative check.
2. Otherwise the files are parsed with ``lib2to3``'s grammar, which is (very
   nearly) the Python 2.7 grammar -- an f-string, a keyword-only argument or
   an annotation fails to parse there.
3. On top of parsing, an AST-free textual scan rejects constructs that parse
   but are not valid on 2.7: f-strings, ``nonlocal``, ``yield from``,
   ``async``/``await``, ``print(...)``-only files, ``urllib.request``,
   ``pathlib``, ``subprocess.run``, pathlib-style ``open(...)`` encoding
   keywords, and ``typing`` imports.

Usage
-----
    python3 tools/py2_syntax_check.py [files...]
"""

from __future__ import print_function

import io
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_FILES = [
    "aquasnowllm.py",
    "setup.py",
    "tests/net_test.py",
]

#: Constructs that either do not exist in 2.7 or produce subtly wrong
#: behaviour there.  Each entry is (label, compiled regex, allowed-in-regex).
FORBIDDEN = [
    ("f-string", re.compile(r"""(?<![\w'"])[fF]['"]"""), None),
    ("nonlocal", re.compile(r"^\s*nonlocal\s+\w", re.MULTILINE), None),
    ("yield from", re.compile(r"\byield\s+from\b"), None),
    ("async/await", re.compile(r"^\s*(async\s+def|await\s)", re.MULTILINE),
     None),
    ("urllib.request", re.compile(r"\burllib\.request\b"), None),
    ("urllib.parse import", re.compile(r"\bfrom\s+urllib\.parse\b"), None),
    ("pathlib", re.compile(r"\bpathlib\b"), None),
    ("subprocess.run", re.compile(r"\bsubprocess\.run\b"), None),
    ("subprocess.DEVNULL", re.compile(r"\bsubprocess\.DEVNULL\b"), None),
    ("typing import", re.compile(r"\bfrom\s+typing\s+import\b"), None),
    ("os.replace", re.compile(r"\bos\.replace\b"), None),
    ("exist_ok", re.compile(r"exist_ok\s*="), None),
    ("encoding= on open()", re.compile(r"""\bopen\([^)\n]*\bencoding\s*="""),
     None),
    ("FileNotFoundError", re.compile(r"\bFileNotFoundError\b"), None),
    ("str.format_map", re.compile(r"\.format_map\("), None),
    ("{**unpacking", re.compile(r"\{\s*\*\*\w"), None),
    ("print(..., file=)", re.compile(r"\bpy3_print\b"), None),
]


def find_python27():
    candidates = []
    found = shutil.which("python2.7") if hasattr(shutil, "which") else None
    if found:
        candidates.append(found)
    for folder in ("/usr/bin", "/opt/local/bin", "/usr/local/bin",
                   "/opt/homebrew/bin"):
        candidates.append(os.path.join(folder, "python2.7"))
    candidates.append("/tmp/py27/bin/python2.7")
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and \
                os.access(candidate, os.X_OK):
            try:
                probe = [candidate, "-c",
                         "import sys; print(sys.version_info[0])"]
                version = subprocess.check_output(probe,
                                                  stderr=subprocess.STDOUT)
            except Exception:
                continue
            if version.strip() in (b"2", "2"):
                return candidate
    return None


def compile_with(python, path):
    process = subprocess.Popen(
        [python, "-c",
         "import sys, py_compile; py_compile.compile(sys.argv[1], "
         "doraise=True)", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = process.communicate()
    return process.returncode, (out + err).decode("utf-8", "replace")


def parse_with_lib2to3(path):
    try:
        from lib2to3 import pygram, pytree
        from lib2to3.pgen2 import driver
    except ImportError:
        return None, "lib2to3 is unavailable; cannot parse-check"
    grammar = pygram.python_grammar_no_print_statement
    # the plain 2.7 grammar accepts ``print x``; use it for accuracy
    grammar = pygram.python_grammar
    parser_driver = driver.Driver(grammar, convert=pytree.convert)
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            parser_driver.parse_string(handle.read())
        return True, ""
    except Exception as exc:
        return False, str(exc)


#: Lines/blocks that are allowed to mention Python 3 APIs, because they sit
#: behind an explicit interpreter guard (the app runs on both, so its
#: ``else:`` branches for Python 3 legitimately import ``urllib.request``).
GUARD_PATTERN = re.compile(
    r"if\s+PY2\b|if\s+sys\.version_info\[0\]\s*==\s*2|"
    r"except\s+ImportError|#\s*pragma:\s*no\s*cover|"
    r"Python 3|python 3|py3")


def _guarded_lines(lines):
    """Return the set of line numbers inside a guarded block (best effort).

    A guard starts on a line matching GUARD_PATTERN and covers the block
    that follows it: everything until a line that is indented *less* than
    the guard's own indentation, or a top level ``def``/``class``.
    """
    guarded = set()
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not GUARD_PATTERN.search(line):
            index += 1
            continue
        indent = len(line) - len(line.lstrip())
        guarded.add(index + 1)
        cursor = index + 1
        while cursor < total:
            candidate = lines[cursor]
            if not candidate.strip():
                guarded.add(cursor + 1)
                cursor += 1
                continue
            candidate_indent = len(candidate) - len(candidate.lstrip())
            stripped = candidate.lstrip()
            if candidate_indent <= indent and not stripped.startswith(
                    ("else", "elif", "except", "finally")):
                break
            # a following clause of the same guard
            if candidate_indent == indent and stripped.startswith(
                    ("else", "elif", "except", "finally")):
                guarded.add(cursor + 1)
                cursor += 1
                continue
            guarded.add(cursor + 1)
            cursor += 1
        index = max(cursor, index + 1)
    return guarded


def textual_scan(path):
    problems = []
    with io.open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    lines = text.split("\n")
    guarded = _guarded_lines(lines)
    for label, pattern, _allowed in FORBIDDEN:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            if line in guarded:
                continue
            problems.append("%s:%d: %s" % (path, line, label))
    return problems


def main(argv):
    files = [arg for arg in argv[1:] if not arg.startswith("-")]
    if not files:
        files = DEFAULT_FILES
    paths = [path if os.path.isabs(path) else os.path.join(ROOT, path)
             for path in files]
    missing = [path for path in paths if not os.path.isfile(path)]
    if missing:
        print("missing file(s): %s" % ", ".join(missing))
        return 2

    python27 = find_python27()
    failures = []
    print("Python 2.7 checkpoint")
    print("  interpreter: %s" % (python27 or "not found (using lib2to3)"))
    for path in paths:
        printable = os.path.relpath(path, ROOT)
        if python27:
            code, output = compile_with(python27, path)
            if code != 0:
                failures.append(printable)
                print("  FAIL %s\n%s" % (printable, output.strip()))
                continue
            print("  ok   %s compiles under CPython 2.7" % printable)
        else:
            parsed, error = parse_with_lib2to3(path)
            if parsed is False:
                failures.append(printable)
                print("  FAIL %s: %s" % (printable, error))
                continue
            print("  ok   %s parses with the 2.7 grammar" % printable)
        for problem in textual_scan(path):
            failures.append(problem)
            print("  FAIL %s" % problem)

    # The application must also stay valid Python 3, so the modern-machine
    # test harnesses can import it.
    for path in paths:
        printable = os.path.relpath(path, ROOT)
        try:
            with io.open(path, "r", encoding="utf-8") as handle:
                compile(handle.read(), path, "exec")
        except SyntaxError as exc:
            failures.append("%s: %s" % (printable, exc))
            print("  FAIL %s is not Python 3 parseable: %s"
                  % (printable, exc))
        else:
            print("  ok   %s also parses as Python 3" % printable)

    print("")
    if failures:
        print("%d problem(s) found." % len(failures))
        return 1
    print("Python 2.7 compatibility verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

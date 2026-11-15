#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a double-clickable ``AquaSnowLLM.app`` from any machine.

What this produces
------------------
A normal Mac application bundle -- ``Contents/Info.plist``, a launcher in
``Contents/MacOS/``, the Python sources in ``Contents/Resources/`` -- with
a shell launcher that finds a Python and starts the app.  Because it can
be built without macOS, the bundle is produced here and shipped as a
release asset.

Which edition it runs
---------------------
The **tkinter** front end (``aquasnow.py``) is the default.  That choice
matters: Python's Tk ships with the interpreter, so such a bundle needs
*nothing* installed.  On OS X 10.6 the system Python is 2.6.1, which is
too old, so 10.6 users do need a Python 2.7 -- but from Lion onward
``/usr/bin/python2.7`` is present and the app runs on a stock system.

The PyQt4 front end can also be bundled, with ``--edition qt``; that one
additionally needs PyQt4 installed, so it is the less portable choice.

What is *not* here
------------------
Freezing (copying the interpreter inside the app) still requires macOS
and is what ``setup.py`` + py2app does.  A frozen bundle is bigger but
runs with no Python installed at all.

Usage
-----
    python3 tools/build_app.py                     # -> dist/AquaSnowLLM.app
    python3 tools/build_app.py --zip               # ...and a zip of it
    python3 tools/build_app.py --edition qt        # the PyQt4 front end
    python3 tools/build_app.py --output /tmp/x     # somewhere else
    python3 tools/build_app.py --bundle-id com.example.aquasnowllm

The bundle is portable: it stores no absolute build paths, so it can be
built on any machine with Python 3 and copied to a Mac.
"""

from __future__ import print_function

import argparse
import os
import plistlib
import shutil
import stat
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_NAME = "AquaSnowLLM"
VERSION = "1.1.0"
EXECUTABLE = "AquaSnowLLM"
BUNDLE_ID = "com.aquasnowllm.app"

#: The two front ends.  ``main`` is the module the launcher runs, ``extra``
#: are the files it needs beside it.
EDITIONS = {
    "tk": {
        "main": "aquasnow.py",
        "extra": ["aquasnowllm.py"],   # shared core: API, TLS, bridge, store
        "needs": "",                   # nothing beyond the interpreter
        # One line, double quotes only: the launcher splices this into a
        # single-quoted shell string and into a comment.  exec keeps it on
        # one line while still using try/except, and it really imports the
        # module, so a Python whose Tk cannot load is rejected too -- not
        # merely one where the module is absent.
        "probe": ('exec("try:\\n import Tkinter\\n'
                  'except ImportError:\\n import tkinter")'),
        "missing_title": "AquaSnowLLM needs Python 2.7",
        "missing_body": (
            # no apostrophes: this is embedded in a single-quoted
            # AppleScript string by the launcher
            "The Python on this Mac is too old for AquaSnowLLM "
            "(Mac OS X 10.6 ships Python 2.6.1).\\n\\n"
            "Install Python 2.7:\\n\\n"
            "  1. Install MacPorts from macports.org\\n"
            "  2. sudo port install python27\\n\\n"
            "Then open AquaSnowLLM again.  See README.md."
        ),
    },
    "qt": {
        "main": "aquasnowllm.py",
        "extra": [],
        "needs": "PyQt4",
        "probe": "import PyQt4.QtGui",
        "missing_title": "AquaSnowLLM needs PyQt4",
        "missing_body": (
            "Install it with:\\n\\n"
            "  sudo port install python27 py27-pyqt4\\n\\n"
            "Then open AquaSnowLLM again.  See README.md."
        ),
    },
}

#: Copied only when present (a user-supplied CA bundle or an icon).
OPTIONAL_RESOURCES = [
    "assets/cacert.pem",
    "assets/AquaSnowLLM.icns",
]

LAUNCHER_TEMPLATE = r'''#!/bin/sh
# AquaSnowLLM launcher.
#
# This bundle is not frozen: it runs the machine's own Python.  Every path
# is relative to this script, so the bundle can be moved or copied between
# Macs without breaking.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
RESOURCES="$HERE/../Resources"
APP="$RESOURCES/__MAIN__"

if [ ! -f "$APP" ]; then
    osascript -e 'display alert "AquaSnowLLM is damaged" message "The application file is missing from the bundle. Download it again."' >/dev/null 2>&1 || true
    echo "missing: $APP" >&2
    exit 1
fi

# A GUI app started by double-click gets a bare environment: no PATH from
# a shell profile, and MacPorts is not on the default PATH.
PATH="/opt/local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

# Pick a Python that can actually run the app.  __PROBE__ is the test: on
# 10.6 the system python is 2.6 and fails it, so MacPorts wins.
PYTHON=""
for candidate in \
    /opt/local/bin/python2.7 \
    /usr/local/bin/python2.7 \
    /usr/bin/python2.7 \
    python2.7 \
    /opt/local/bin/python \
    python
do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c '__PROBE__' >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    osascript -e 'display alert "__MISSING_TITLE__" message "__MISSING_BODY__"' >/dev/null 2>&1 || true
    echo "__MISSING_TITLE__" >&2
    exit 2
fi

# exec so the app owns the process: quitting the window quits the app.
exec "$PYTHON" "$APP" "$@"
'''

INFO_PLIST = {
    "CFBundleName": APP_NAME,
    "CFBundleDisplayName": APP_NAME,
    "CFBundleExecutable": EXECUTABLE,
    "CFBundleIdentifier": BUNDLE_ID,
    "CFBundleInfoDictionaryVersion": "6.0",
    "CFBundlePackageType": "APPL",
    "CFBundleSignature": "????",
    "CFBundleVersion": VERSION,
    "CFBundleShortVersionString": VERSION,
    "CFBundleGetInfoString": "%s %s -- OpenRouter chat client for old Macs"
                             % (APP_NAME, VERSION),
    "NSHumanReadableCopyright": "https://github.com/aquasnowllm",
    "LSMinimumSystemVersion": "10.6",
    "NSPrincipalClass": "NSApplication",
    "NSHighResolutionCapable": True,
    # The app speaks TLS itself with ssl/urllib2 (and the local bridge
    # speaks plain HTTP), so App Transport Security must not interfere.
    "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": True},
    "LSArchitecturePriority": ["x86_64", "i386"],
}


def read_version():
    """Take the version from the app itself, so the two cannot drift."""
    path = os.path.join(ROOT, "aquasnowllm.py")
    with open(path, "r") as handle:
        for line in handle:
            if line.startswith("APP_VERSION"):
                return line.split("=", 1)[1].strip().strip("\"'")
    return VERSION


def build_launcher(edition):
    spec = EDITIONS[edition]
    text = LAUNCHER_TEMPLATE
    text = text.replace("__MAIN__", spec["main"])
    text = text.replace("__PROBE__", spec["probe"])
    text = text.replace("__MISSING_TITLE__", spec["missing_title"])
    text = text.replace("__MISSING_BODY__", spec["missing_body"])
    return text


def build(output_dir, edition="tk"):
    version = read_version()
    spec = EDITIONS[edition]
    app = os.path.join(output_dir, APP_NAME + ".app")
    contents = os.path.join(app, "Contents")
    macos = os.path.join(contents, "MacOS")
    resources = os.path.join(contents, "Resources")

    if os.path.isdir(app):
        shutil.rmtree(app)
    for folder in (macos, resources):
        os.makedirs(folder)

    # 1. the launcher
    launcher = os.path.join(macos, EXECUTABLE)
    with open(launcher, "w") as handle:
        handle.write(build_launcher(edition))
    os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR |
             stat.S_IXGRP | stat.S_IXOTH)

    # 2. Info.plist (XML plists are fine on 10.6)
    plist = dict(INFO_PLIST)
    plist["CFBundleVersion"] = version
    plist["CFBundleShortVersionString"] = version
    plist["CFBundleGetInfoString"] = (
        "%s %s -- OpenRouter chat client for old Macs" % (APP_NAME, version))
    with open(os.path.join(contents, "Info.plist"), "wb") as handle:
        plistlib.dump(plist, handle, fmt=plistlib.FMT_XML)

    # 3. the payload
    copied = []
    for relative in [spec["main"]] + spec["extra"]:
        source = os.path.join(ROOT, relative)
        if not os.path.isfile(source):
            raise SystemExit("missing required file: %s" % relative)
        shutil.copy2(source, os.path.join(resources,
                                          os.path.basename(relative)))
        copied.append(relative)
    for relative in OPTIONAL_RESOURCES:
        source = os.path.join(ROOT, relative)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(resources,
                                              os.path.basename(relative)))
            copied.append(relative)

    # 4. PkgInfo, which 10.6 era bundles carry
    with open(os.path.join(contents, "PkgInfo"), "w") as handle:
        handle.write("APPL????")

    return app, copied, spec


def zip_app(app, zip_path):
    """Zip the bundle, keeping the ``.app`` directory and modes intact."""
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder, _dirs, files in os.walk(app):
            for name in sorted(files):
                full = os.path.join(folder, name)
                relative = os.path.relpath(full, os.path.dirname(app))
                info = zipfile.ZipInfo(relative)
                mode = os.stat(full).st_mode
                info.external_attr = (mode & 0xFFFF) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(full, "rb") as handle:
                    archive.writestr(info, handle.read())
    return zip_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--output", default=os.path.join(ROOT, "dist"),
                        help="where to create the .app (default: dist/)")
    parser.add_argument("--edition", default="tk", choices=sorted(EDITIONS),
                        help="which front end to bundle (default: tk, "
                             "which needs nothing installed)")
    parser.add_argument("--bundle-id", default=BUNDLE_ID,
                        help="CFBundleIdentifier (default: %(default)s)")
    parser.add_argument("--zip", action="store_true",
                        help="also write AquaSnowLLM.app.zip next to it")
    args = parser.parse_args(argv)

    if args.bundle_id != BUNDLE_ID:
        INFO_PLIST["CFBundleIdentifier"] = args.bundle_id

    if not os.path.isdir(args.output):
        os.makedirs(args.output)
    app, copied, spec = build(args.output, args.edition)

    needs = spec["needs"] or "nothing (Python's own Tk)"
    print("%s.app" % APP_NAME)
    print("  edition   : %s (%s)" % (args.edition, spec["main"]))
    print("  needs     : %s" % needs)
    print("  version   : %s" % read_version())
    print("  bundle id : %s" % INFO_PLIST["CFBundleIdentifier"])
    print("  location  : %s" % app)
    print("  payload   :")
    for relative in copied:
        print("    Contents/Resources/%s" % os.path.basename(relative))

    if args.zip:
        path = zip_app(app, os.path.join(args.output, APP_NAME + ".app.zip"))
        print("  archive   : %s" % path)

    print("")
    print("Double-click on a Mac with Python 2.7 (Lion and newer have it).")
    print("For a bundle that carries its own interpreter, run")
    print("`python2.7 setup.py py2app` on a Mac instead.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a double-clickable ``AquaSnowLLM.app`` without py2app.

Why this exists
---------------
py2app gives you a *frozen* bundle: it copies a Python framework and the
Qt 4 libraries inside the app, so the Mac needs nothing installed.  That
step genuinely requires macOS -- it links against the local Python and
Qt dylibs, and on Snow Leopard it must be built on 10.6 to use the 10.6
SDK.  ``setup.py`` does that when you run it on a Mac.

This script builds the other half: the bundle *structure* (``Contents/
Info.plist``, ``Contents/MacOS/``, ``Contents/Resources/``) plus a
launcher that finds the system Python 2.7 and PyQt4 and runs the app.
Mac OS X 10.6 ships Python 2.7 in ``/usr/bin``, so on a Snow Leopard Mac
that already has PyQt4 installed (one MacPorts command) this bundle
works when double-clicked -- and it is also the exact same directory
tree py2app produces, so anything that inspects the bundle is happy.

The result is a real ``.app`` you can zip and hand to someone.

Usage
-----
    python3 tools/build_app.py                  # -> dist/AquaSnowLLM.app
    python3 tools/build_app.py --output /tmp/x  # somewhere else
    python3 tools/build_app.py --bundle-id com.example.aquasnowllm

The bundle is portable: it stores no absolute paths, so it can be built
on any machine with Python 3 and copied to a Mac.
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
VERSION = "1.0.0"
EXECUTABLE = "AquaSnowLLM"
BUNDLE_ID = "com.aquasnowllm.app"

#: Files that are shipped inside Contents/Resources.
RESOURCES = [
    "aquasnowllm.py",
    "assets/README.txt",
]

#: Copied only when present (a user-supplied CA bundle or icon).
OPTIONAL_RESOURCES = [
    "assets/cacert.pem",
    "assets/AquaSnowLLM.icns",
]

LAUNCHER = r'''#!/bin/sh
# AquaSnowLLM launcher.
#
# This bundle is not frozen: it runs the Python 2.7 interpreter that ships
# with Mac OS X 10.6 and newer.  PyQt4 (or PySide) has to be installed --
# see the README.  Every path is relative to this script, so the bundle can
# be moved or copied between Macs.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
RESOURCES="$HERE/../Resources"
APP="$RESOURCES/aquasnowllm.py"

if [ ! -f "$APP" ]; then
    osascript -e 'display alert "AquaSnowLLM is damaged" message "aquasnowllm.py is missing from the bundle. Download it again."' >/dev/null 2>&1 || true
    echo "aquasnowllm.py is missing from $RESOURCES" >&2
    exit 1
fi

# A GUI app started by double-click inherits a bare environment: no
# PATH from the shell profile, and MacPorts is not in the default PATH.
PATH="/opt/local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

# Pick an interpreter that has a Qt 4 binding.  /usr/bin/python2.7 is the
# Snow Leopard system Python; the MacPorts one knows where its PyQt4 is.
PYTHON=""
for candidate in \
    /opt/local/bin/python2.7 \
    /usr/local/bin/python2.7 \
    /usr/bin/python2.7 \
    python2.7 \
    python
do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import PyQt4.QtGui' >/dev/null 2>&1 || \
       "$candidate" -c 'import PySide.QtGui' >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    # Nothing works: run the best Python we have so the app can explain
    # itself, and show a real dialog because there is no terminal.
    if command -v /usr/bin/python2.7 >/dev/null 2>&1; then
        PYTHON=/usr/bin/python2.7
    else
        PYTHON=python
    fi
    osascript -e 'display alert "AquaSnowLLM needs PyQt4" message "Install it with:  sudo port install py27-pyqt4\n\nThen open AquaSnowLLM again.  See README.md for details."' >/dev/null 2>&1 || true
fi

# ``exec`` so the Dock shows the Python icon as *this* app, and quitting
# the window really quits.
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
    # Snow Leopard's LaunchServices honours this; it also documents intent.
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


def build(output_dir):
    version = read_version()
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
        handle.write(LAUNCHER)
    os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR |
             stat.S_IXGRP | stat.S_IXOTH)

    # 2. Info.plist (binary plists are not needed; XML is fine on 10.6)
    plist = dict(INFO_PLIST)
    plist["CFBundleVersion"] = version
    plist["CFBundleShortVersionString"] = version
    plist["CFBundleGetInfoString"] = (
        "%s %s -- OpenRouter chat client for old Macs" % (APP_NAME, version))
    plist_path = os.path.join(contents, "Info.plist")
    with open(plist_path, "wb") as handle:
        plistlib.dump(plist, handle, fmt=plistlib.FMT_XML)

    # 3. the payload
    copied = []
    for relative in RESOURCES:
        source = os.path.join(ROOT, relative)
        if not os.path.isfile(source):
            raise SystemExit("missing required file: %s" % relative)
        destination = os.path.join(resources, os.path.basename(relative))
        shutil.copy2(source, destination)
        copied.append(relative)
    for relative in OPTIONAL_RESOURCES:
        source = os.path.join(ROOT, relative)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(resources,
                                              os.path.basename(relative)))
            copied.append(relative)

    # 4. a PkgInfo, which is what 10.6 era bundles carry
    with open(os.path.join(contents, "PkgInfo"), "w") as handle:
        handle.write("APPL????")

    return app, copied


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
    parser.add_argument("--bundle-id", default=BUNDLE_ID,
                        help="CFBundleIdentifier (default: %(default)s)")
    parser.add_argument("--zip", action="store_true",
                        help="also write AquaSnowLLM.app.zip next to it")
    args = parser.parse_args(argv)

    if args.bundle_id != BUNDLE_ID:
        INFO_PLIST["CFBundleIdentifier"] = args.bundle_id

    if not os.path.isdir(args.output):
        os.makedirs(args.output)
    app, copied = build(args.output)

    print("%s.app" % APP_NAME)
    print("  bundle id : %s" % INFO_PLIST["CFBundleIdentifier"])
    print("  version   : %s" % read_version())
    print("  location  : %s" % app)
    print("  payload   :")
    for relative in copied:
        print("    Contents/Resources/%s" % os.path.basename(relative))

    if args.zip:
        path = zip_app(app, os.path.join(args.output, APP_NAME + ".app.zip"))
        print("  archive   : %s" % path)

    print("")
    print("Double-click the bundle on a Mac with Python 2.7 and PyQt4.")
    print("For a fully self-contained bundle, run `python2.7 setup.py py2app`")
    print("on the Mac instead -- that freezes Python and Qt inside the app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

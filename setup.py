#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""py2app packaging for AquaSnowLLM (Python 2.7, Mac OS X 10.6+).

Building a frozen ``.app``
--------------------------
This freezes **aquasnow.py**, the tkinter edition.  Tk is part of Python
itself, so unlike the PyQt4 front end there is no Qt to install: py2app
is the only packaging dependency.

    # one time, on the Snow Leopard machine that will build the bundle
    sudo port install python27
    sudo port install py27-py2app      # the packager
    sudo port select --set python python27

    # confirm Tk is importable by the interpreter that will build
    python2.7 -c "import Tkinter; print('Tk OK')"

    # from this directory
    python2.7 setup.py py2app

For the PyQt4 front end, see ``tools/build_app.py --edition qt``, or
change ``APP`` below to ``aquasnowllm.py`` and install ``py27-pyqt4``
first -- a bundle built without it starts and dies on the Qt import.

If py2app comes from pip rather than MacPorts, pin it: 0.28.9.  Version
0.28.10 declares requires_python >= 3.10 and will not install on 2.7.

The result is ``dist/AquaSnowLLM.app`` -- a double-clickable, standalone
bundle.  It can be zipped and copied to other 10.6+ Macs.

Notes for Snow Leopard
----------------------
* Build on the *oldest* OS you intend to support: a bundle produced on
  10.6 runs on 10.6 and newer, not the other way around.
* Build with the Apple-supplied Python (``/usr/bin/python2.7``) if you want
  the bundle to rely on nothing but the system frameworks.  The MacPorts
  Python works too, but then the bundle carries its own interpreter, which
  makes it much bigger -- and MacPorts Python 2.7 is built for the OS it was
  installed on.
* ``arch`` is set below to ``x86_64`` (10.6 on modern Intel hardware).
  For a 32-bit Core Duo Mac use ``arch=["i386"]``; for a Universal binary
  build both ``i386`` and ``x86_64`` and pass ``arch=["i386", "x86_64"]``.
* Any ``cacert.pem`` next to this file is bundled into
  ``AquaSnowLLM.app/Contents/Resources/``; the app finds it automatically.
"""

from __future__ import print_function

import os
import sys

from setuptools import setup

APP = ["aquasnow.py"]   # the tkinter edition: no Qt needed
APP_NAME = "AquaSnowLLM"
VERSION = "1.1.0"

HERE = os.path.dirname(os.path.abspath(__file__))

#: Extra files that end up in Contents/Resources (looked up via
#: aquasnowllm.find_bundle_file()).
resources = ["assets/README.txt"]
optional_resources = ["assets/cacert.pem", "assets/AquaSnowLLM.icns"]
for candidate in optional_resources:
    if os.path.exists(os.path.join(HERE, candidate)):
        resources.append(candidate)

#: py2app hook niceties.  ``requests`` is *not* required (the app uses
#: urllib2/ssl from the standard library) but is bundled when installed.
packages = []
includes = []
if sys.version_info[0] == 2:
    includes.append("urllib2")
    includes.append("httplib")
try:  # optional: the app itself only needs the standard library
    import requests
except ImportError:
    requests = None
else:  # pragma: no cover - depends on the build machine
    packages.append("requests")

plist = {
    "CFBundleName": APP_NAME,
    "CFBundleDisplayName": APP_NAME,
    "CFBundleIdentifier": "com.aquasnowllm.app",
    "CFBundleVersion": VERSION,
    "CFBundleShortVersionString": VERSION,
    "CFBundleGetInfoString": "%s %s -- OpenRouter chat client for old Macs"
                             % (APP_NAME, VERSION),
    "NSHumanReadableCopyright": "https://github.com/aquasnowllm",
    "LSMinimumSystemVersion": "10.6",
    "NSHighResolutionCapable": True,
    # The app does its own TLS with ssl/urllib2; no ATS requirement.
    "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": True},
}

options = {
    "argv_emulation": False,   # 10.6+ passes file open events fine
    "iconfile": None,
    "plist": plist,
    "packages": packages,
    "includes": includes,
    "excludes": ["PyQt4", "PyQt5", "PySide", "PySide2", "PyQt6"],
    "resources": resources,
    "arch": ["x86_64"],
    "semi_standalone": False,
}

if os.path.exists(os.path.join(HERE, "assets", "AquaSnowLLM.icns")):
    options["iconfile"] = os.path.join("assets", "AquaSnowLLM.icns")

setup(
    app=APP,
    name=APP_NAME,
    version=VERSION,
    description="Aqua chat client for OpenRouter, for Mac OS X 10.6+",
    long_description=(
        "A lightweight chat client for the OpenRouter API that runs on "
        "Mac OS X 10.6 Snow Leopard and newer. Supports free API keys and "
        ":free models, and ships an HTTP-to-HTTPS bridge for machines whose "
        "OpenSSL is too old to complete a modern TLS handshake."),
    author="AquaSnowLLM contributors",
    url="https://github.com/aquasnowllm",
    license="MIT",
    options={"py2app": options},
    setup_requires=["py2app"],
)

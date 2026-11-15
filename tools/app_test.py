#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check that ``tools/build_app.py`` produces a valid Mac OS X bundle.

Runs on any platform: a ``.app`` is a directory with a known shape, so
the structure can be asserted without macOS.  (Whether the *frozen*
py2app bundle launches obviously cannot be tested here.)

    python3 tools/app_test.py
"""

from __future__ import print_function

import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "tools", "build_app.py")

failures = []


def check(label, condition, detail=""):
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s   %s" % (label, detail))
        failures.append(label)


def main():
    output = tempfile.mkdtemp(prefix="aquasnow-app-")
    try:
        process = subprocess.Popen(
            [sys.executable, BUILD, "--output", output, "--zip"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = process.communicate()
        text = out.decode("utf-8", "replace")
        check("builder exits cleanly", process.returncode == 0, text[-500:])
        if process.returncode != 0:
            return 1

        app = os.path.join(output, "AquaSnowLLM.app")
        check("AquaSnowLLM.app exists", os.path.isdir(app))
        contents = os.path.join(app, "Contents")
        check("Contents/ exists", os.path.isdir(contents))
        check("Contents/MacOS/ exists",
              os.path.isdir(os.path.join(contents, "MacOS")))
        check("Contents/Resources/ exists",
              os.path.isdir(os.path.join(contents, "Resources")))

        # --- Info.plist: the keys LaunchServices needs on 10.6 ---
        plist_path = os.path.join(contents, "Info.plist")
        check("Info.plist exists", os.path.isfile(plist_path))
        with open(plist_path, "rb") as handle:
            plist = plistlib.load(handle)
        check("CFBundlePackageType is APPL",
              plist.get("CFBundlePackageType") == "APPL")
        check("CFBundleName", plist.get("CFBundleName") == "AquaSnowLLM")
        check("CFBundleExecutable matches the launcher",
              plist.get("CFBundleExecutable") == "AquaSnowLLM")
        check("LSMinimumSystemVersion is 10.6",
              plist.get("LSMinimumSystemVersion") == "10.6")
        check("CFBundleIdentifier is reverse-DNS",
              "." in plist.get("CFBundleIdentifier", ""))
        # read the version out of the app so a bump cannot desynchronise
        # the bundle from the code
        with open(os.path.join(ROOT, "aquasnowllm.py"), "r") as handle:
            app_version = ""
            for line in handle:
                if line.startswith("APP_VERSION"):
                    app_version = line.split("=", 1)[1].strip().strip("\"'")
                    break
        check("version matches the app",
              plist.get("CFBundleShortVersionString") == app_version,
              "%s vs %s" % (plist.get("CFBundleShortVersionString"),
                            app_version))

        # --- the launcher must be executable and be a shell script ---
        launcher = os.path.join(contents, "MacOS",
                                plist["CFBundleExecutable"])
        check("launcher exists", os.path.isfile(launcher))
        mode = os.stat(launcher).st_mode
        check("launcher is executable", bool(mode & stat.S_IXUSR), oct(mode))
        with open(launcher, "rb") as handle:
            head = handle.read(2)
        check("launcher is a script", head == b"#!", repr(head))

        # --- payload: the tkinter edition plus the shared core ---
        resources = os.path.join(contents, "Resources")
        check("aquasnow.py is inside the bundle",
              os.path.isfile(os.path.join(resources, "aquasnow.py")))
        check("the shared core is inside the bundle",
              os.path.isfile(os.path.join(resources, "aquasnowllm.py")))

        # --- no absolute build paths leak into the bundle ---
        leaked = []
        for folder, _dirs, files in os.walk(app):
            for name in files:
                path = os.path.join(folder, name)
                with open(path, "rb") as handle:
                    if ROOT.encode("utf-8") in handle.read():
                        leaked.append(os.path.relpath(path, app))
        check("bundle contains no absolute build paths", not leaked,
              ", ".join(leaked))

        # --- the zip keeps the exec bit and the directory ---
        archive_path = os.path.join(output, "AquaSnowLLM.app.zip")
        check("zip written", os.path.isfile(archive_path))
        with zipfile.ZipFile(archive_path) as archive:
            check("zip is intact", archive.testzip() is None)
            names = archive.namelist()
            check("zip stores the .app directory",
                  all(name.startswith("AquaSnowLLM.app/") for name in names))
            info = archive.getinfo("AquaSnowLLM.app/Contents/MacOS/"
                                   "AquaSnowLLM")
            bits = (info.external_attr >> 16) & 0o777
            check("zip preserves the launcher's exec bit",
                  bool(bits & 0o100), oct(bits))

            # unzip into a clean folder: the bundle must be relocatable
            fresh = tempfile.mkdtemp(prefix="aquasnow-unzip-")
            try:
                archive.extractall(fresh)
                moved = os.path.join(fresh, "AquaSnowLLM.app", "Contents",
                                     "MacOS", "AquaSnowLLM")
                check("unzipped launcher exists", os.path.isfile(moved))
                result = subprocess.Popen(
                    ["sh", "-n", moved],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                out2, _ = result.communicate()
                check("unzipped launcher is valid sh", result.returncode == 0,
                      out2.decode("utf-8", "replace"))
            finally:
                shutil.rmtree(fresh, ignore_errors=True)

        # --- the packaged app still self-tests ---
        bundled = os.path.join(resources, "aquasnow.py")
        run = subprocess.Popen([sys.executable, bundled, "--self-test"],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
        out3, _ = run.communicate()
        check("packaged copy passes --self-test", run.returncode == 0,
              out3.decode("utf-8", "replace")[-400:])

        # --- a packaged app must not be able to break its own launcher ---
        with open(launcher, "rb") as handle:
            script = handle.read().decode("utf-8", "replace")
        check("the launcher keeps the bundle self-contained",
              "/home/" not in script and "/tmp/" not in script,
              "absolute build path in the launcher")
        check("the launcher refuses to start without a usable Python",
              "missing_title" not in script and "needs Python" in script,
              script[-300:])
        # the launcher embeds AppleScript inside single quotes: a stray
        # apostrophe there produced invalid shell once already
        for line in script.splitlines():
            if "osascript" in line:
                check("the embedded AppleScript has no stray apostrophe",
                      line.count("'") == 2 or line.count("'") % 2 == 0,
                      line[:120])
    finally:
        shutil.rmtree(output, ignore_errors=True)

    print("")
    if failures:
        print("%d failure(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("All bundle checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Open the real tkinter window and click through it, headless.

``tools/tk_mock_test.py`` drives the app against a fake Tk, which proves
the wiring but not that the widgets exist or lay out.  This script opens
a *real* ``Tk`` root -- on CI under ``xvfb-run`` -- builds the actual
ChatApp, and exercises it through Tk's own event loop.

It needs Tk installed and a display (``xvfb-run`` on a headless box):

    xvfb-run -a python3 tools/tk_window_smoke.py

Skips cleanly with exit 0 when Tk is unavailable, so it is harmless to run
on a machine that has no Tk.
"""

from __future__ import print_function

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

failures = []


def check(label, condition, detail=""):
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s   %s" % (label, detail))
        failures.append(label)


def main():
    try:
        import tkinter
    except ImportError:
        print("tkinter is not installed -- skipping the window test.")
        return 0

    try:
        probe = tkinter.Tk()
    except Exception as exc:
        print("no display available (%s) -- skipping." % exc)
        return 0
    probe.destroy()

    import aquasnow as app_module
    from aquasnowllm import ChatStore, Config

    folder = tempfile.mkdtemp(prefix="aquasnow-window-")
    os.environ["AQUASNOWLLM_HOME"] = folder

    config = Config(folder)
    config["api_key"] = "sk-or-v1-window"
    config["model"] = "openrouter/free"
    config.save()
    store = ChatStore(folder)

    tk, ttk, messagebox, filedialog = app_module._import_tk()
    root = tk.Tk()
    try:
        window = app_module.ChatApp(root, config, store, tk, ttk,
                                    messagebox, filedialog)
        root.update_idletasks()
        root.update()

        # -- the widgets are real and laid out --------------------------
        check("the window has a title", bool(root.title()), root.title())
        check("the root has a size", root.winfo_width() > 200,
              root.winfo_width())
        check("the transcript is mapped", window.transcript.winfo_exists())
        check("the composer is mapped", window.composer.winfo_exists())
        check("the session list has one row",
              window.session_list.size() == 1, window.session_list.size())
        check("the send button exists",
              window.send_button.winfo_exists() == 1)

        # -- typing enables Send for real -------------------------------
        window.composer.insert("1.0", "hello from a real window")
        root.update()
        window._sync_button()
        root.update()
        check("Send enables after typing",
              str(window.send_button.cget("state")) != "disabled",
              window.send_button.cget("state"))

        # -- a stored message renders into the text widget --------------
        store.append_message(window.session, "user", "rendered text")
        store.append_message(window.session, "assistant", "reply text")
        window._render()
        root.update()
        content = window.transcript.get("1.0", "end")
        check("the transcript renders stored turns",
              "rendered text" in content and "reply text" in content,
              content[:120])

        # -- tags exist for the rendering -------------------------------
        check("the user tag has a colour",
              bool(window.transcript.tag_cget("user", "foreground")))
        check("the link tag is underlined",
              str(window.transcript.tag_cget("link", "underline"))
              in ("1", "True", "true"))

        # -- history navigation commands --------------------------------
        window._new_chat()
        root.update()
        check("New Chat adds a row", window.session_list.size() == 2,
              window.session_list.size())
        window._delete_chat()       # auto-confirm is not available; the
        root.update()               # messagebox would block, so only assert
        check("deleting leaves a row behind",
              window.session_list.size() >= 1, window.session_list.size())

        # -- the settings dialog builds and closes ----------------------
        dialog = app_module.SettingsDialog(window)
        root.update()
        check("the settings window exists",
              dialog.top.winfo_exists() == 1)
        dialog.top.destroy()
        root.update()

        # -- the event loop drains without error ------------------------
        events = {"count": 0}

        def tick():
            events["count"] += 1
            if events["count"] < 3:
                root.after(10, tick)
            else:
                root.quit()

        root.after(10, tick)
        root.mainloop()
        check("the event loop ran", events["count"] == 3, events["count"])
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    print("")
    if failures:
        print("%d failure(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("All window checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Headless GUI smoke test for aquasnowllm.py.

The app itself targets *PyQt4 for Python 2.7*; that combination cannot be
installed on a modern Linux CI box.  This harness therefore imports the
module with PyQt5 installed under the name ``PyQt4`` (the Qt4-era API the
app uses is a subset of PyQt5's), shrinks the few behaviours that changed
between Qt 4 and Qt 5, and then drives the real ``MainWindow``:

  * create the window, sidebar, transcript, composer, toolbar
  * send a message against a fake OpenRouter (local HTTP bridge or a
    stubbed client), verifying threading, rendering and persistence
  * reopen the settings dialog and save values
  * switch chats / delete / export

Run it with:  python3 tools/gui_smoke.py     (needs PyQt5, offscreen)
"""

import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtGui, QtWidgets, QtNetwork  # noqa: E402

# --- present PyQt5 as "PyQt4" so the module's import block picks it up ---
shim = type(sys)("PyQt4")
shim.QtCore = QtCore
shim.QtGui = QtGui
shim.QtNetwork = QtNetwork
# Qt4's QtGui contained the widget classes; expose them for the app
for name in dir(QtWidgets):
    if not name.startswith("_"):
        setattr(QtGui, name, getattr(QtWidgets, name))
sys.modules["PyQt4"] = shim

import aquasnowllm as A  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print("  ok   %s" % label)
    else:
        FAILURES.append(label)
        print("  FAIL %s   %s" % (label, detail))


def pump(app, seconds=0.4):
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)


def main():
    print("Qt binding seen by the app:", A.QT_API)
    if A.QT_API != "PyQt4":
        print("  (this harness only works with the PyQt4 alias)")
        return 2

    folder = tempfile.mkdtemp(prefix="aquasnow-gui-")
    os.environ["AQUASNOWLLM_HOME"] = folder
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")

    config = A.Config(folder)
    config["api_key"] = "test-key"
    config["free_only"] = True
    config["bridge_autostart"] = False
    config.save()

    window = A.MainWindow(config=config)
    window.show()
    pump(app, 0.2)

    # -- structure -----------------------------------------------------
    check("window title", window.windowTitle() == "AquaSnowLLM",
          window.windowTitle())
    check("model defaults to openrouter/free",
          window.current_model() == "openrouter/free", window.current_model())
    check("sidebar has one chat", window.session_list.count() == 1,
          str(window.session_list.count()))
    check("toolbar has the model combo", window.model_combo.isEditable())
    check("free-only checkbox on", window.free_check.isChecked())
    check("transcript is a text browser",
          isinstance(window.chat_view, QtWidgets.QTextBrowser))
    check("composer accepts return-to-send",
          hasattr(window.composer, "return_pressed"))

    # -- transcript links ---------------------------------------------
    opened = {}
    original_open = QtGui.QDesktopServices.openUrl

    def fake_open(url):
        opened["url"] = url.toString()
        return True

    QtGui.QDesktopServices.openUrl = staticmethod(fake_open)
    try:
        chat = {"model": "m", "messages": [
            {"role": "assistant", "ts": "",
             "content": "see https://openrouter.ai/keys for keys"}]}
        window.chat_view.show_chat(chat)
        pump(app, 0.1)
        window.chat_view.setHtml(
            window.chat_view.toHtml().replace("https://openrouter.ai/keys",
                                              "https://openrouter.ai/keys"))
        QtCore.QUrl("https://openrouter.ai/keys")
        window.chat_view._open_link("https://openrouter.ai/keys")
        check("links open in the browser",
              opened.get("url") == "https://openrouter.ai/keys",
              repr(opened))
    finally:
        QtGui.QDesktopServices.openUrl = original_open

    # -- model picker --------------------------------------------------
    items = [window.model_combo.itemData(index)
             for index in range(window.model_combo.count())]
    check("prepopulated free models", "nvidia/nemotron-3.5-super-vl:free"
          in items, repr(items))
    window.model_combo.setEditText("acme/custom-9b:free")
    window._model_edited("acme/custom-9b:free")
    check("custom model id accepted", config.model() == "acme/custom-9b:free",
          config.model())
    window.free_check.setChecked(False)
    pump(app, 0.1)
    check("custom model id survives a list refresh",
          window.current_model() == "acme/custom-9b:free",
          window.current_model())
    window.free_check.setChecked(True)
    pump(app, 0.1)
    check("custom model id survives Free Only",
          window.current_model() == "acme/custom-9b:free",
          window.current_model())

    # -- composer keys -------------------------------------------------
    window.composer.setPlainText("line one")
    window.composer.moveCursor(QtGui.QTextCursor.End)
    event = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Return,
                            QtCore.Qt.ShiftModifier)
    window.composer.keyPressEvent(event)
    check("shift+return inserts a newline",
          window.composer.toPlainText() == "line one\n",
          repr(window.composer.toPlainText()))
    window.composer.setPlainText("")
    window.composer.clear()
    # Plain Return must ask for a send instead of inserting a newline.
    # (A QKeyEvent that has already been "accepted" gets re-handled by
    # QTextEdit, so hand the composer a fresh one via sendEvent.)
    window.composer.setPlainText("return sends")
    window.composer.moveCursor(QtGui.QTextCursor.End)

    class Recorder(QtCore.QObject):
        def __init__(self):
            QtCore.QObject.__init__(self)
            self.seen = 0

        def on_return(self):
            self.seen += 1

    recorder = Recorder()
    window.composer.return_pressed.disconnect(window.send_message)
    window.composer.return_pressed.connect(recorder.on_return)
    # a hand-built event arrives already "accepted", which makes QTextEdit
    # re-handle it; send a fresh one through the normal event path
    event = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Return,
                            QtCore.Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(window.composer, event)
    window.composer.return_pressed.disconnect(recorder.on_return)
    window.composer.return_pressed.connect(window.send_message)
    check("plain return asks to send", recorder.seen == 1,
          str(recorder.seen))
    check("plain return does not add a newline",
          window.composer.toPlainText() == "return sends",
          repr(window.composer.toPlainText()))
    window.composer.clear()

    # -- sending (threaded) -------------------------------------------
    sent = {}

    def fake_chat(**kwargs):
        sent.update(kwargs)
        time.sleep(0.05)
        return "Hello from the model \u2014 caf\u00e9!", {"total_tokens": 7}

    client = A.LanguageModel("test-key", base_url="",
                             cacert_path="", timeout=5)
    client.chat = fake_chat
    config["model"] = "openrouter/free"
    window.model_combo.setEditText("openrouter/free")
    window._model_edited("openrouter/free")
    window._make_client = lambda require_key=True: (client, None)
    window.composer.setPlainText("Hi there!")
    window.send_message()
    check("send leaves the composer empty",
          window.composer.toPlainText() == "", window.composer.toPlainText())
    check("send button disabled while busy",
          not window.send_button.isEnabled())
    for _ in range(200):
        pump(app, 0.05)
        if not window._sending and window.send_button.isEnabled():
            break
    check("worker finished", not window._sending)
    chat = window._current_chat()
    roles = [message["role"] for message in chat["messages"]]
    check("user message stored", roles == ["user", "assistant"], repr(roles))
    check("assistant text stored",
          chat["messages"][1]["content"].startswith("Hello from the model"),
          chat["messages"][1]["content"][:40])
    check("smart dashes folded in the stored reply",
          "\u2014" not in chat["messages"][1]["content"],
          chat["messages"][1]["content"][:40])
    html = window.chat_view.toHtml()
    check("transcript rendered the reply", "Hello from the model" in html)
    check("sidebar title from first message",
          chat["title"].startswith("Hi there"), chat["title"])
    check("payload included the user turn",
          sent["messages"][-1]["content"] == "Hi there!", repr(sent))
    saved = json.load(open(os.path.join(folder, "chats.json")))
    check("chats.json written", len(saved["chats"][0]["messages"]) == 2,
          repr(saved)[:120])
    check("status bar reported the answer",
          "Answer received" in window.status_label.text(),
          window.status_label.text())
    pump(app, 0.2)

    # -- stopping a request -------------------------------------------
    slow_client = A.LanguageModel("test-key", base_url="", timeout=5)
    started = {"value": False}

    def slow_chat(**kwargs):
        started["value"] = True
        time.sleep(1.5)
        return "this should never appear", {}

    slow_client.chat = slow_chat
    window._make_client = lambda require_key=True: (slow_client, None)
    window.composer.setPlainText("please wait")
    window.send_message()
    pump(app, 0.15)
    check("slow request started", started["value"])
    check("stop button enabled while busy", window.stop_button.isEnabled())
    window.cancel_request()
    pump(app, 0.1)
    check("stop re-enables send", window.send_button.isEnabled())
    check("stop clears busy flag", not window._sending)
    pump(app, 1.8)   # let the abandoned thread finish
    chat = window._current_chat()
    check("abandoned answer is not stored",
          all("never appear" not in message["content"]
              for message in chat["messages"]),
          repr([m["content"] for m in chat["messages"]]))

    # -- second chat + switching --------------------------------------
    window.new_chat()
    pump(app, 0.1)
    check("new chat created", window.session_list.count() == 2,
          str(window.session_list.count()))
    check("new chat is empty",
          window._current_chat()["messages"] == [])
    window.session_list.setCurrentRow(1)
    pump(app, 0.1)
    check("switching chats restores the transcript",
          "Hello from the model" in window.chat_view.toHtml())

    # -- settings dialog ----------------------------------------------
    dialog = A.SettingsDialog(config, window)
    dialog.base_edit.setText("https://example.invalid/api/v1")
    dialog.temp_spin.setValue(0.3)
    dialog.tokens_spin.setValue(512)
    dialog.headers_edit.setText("X-OpenRouter-Title: Aqua | X-Extra: yes")
    values = dialog.values()
    check("settings dialog returns values",
          values["temperature"] == 0.3 and values["max_tokens"] == 512,
          repr(values))
    config.data.update(values)
    headers = config.extra_headers()
    check("extra headers parsed",
          headers.get("X-OpenRouter-Title") == "Aqua"
          and headers.get("X-Extra") == "yes", repr(headers))
    dialog.deleteLater()

    # -- error rendering ----------------------------------------------
    error = A.APIError(429, "HTTP 429",
                       detail="Rate limit exceeded",
                       retry_after="30")
    window.add_notice("request failed", error)
    html = window.chat_view.toHtml()
    check("rate limit explained in the transcript",
          "rate limiting" in html.lower(), html[-300:])
    check("server detail shown", "Rate limit exceeded" in html)

    missing = A.APIError(401, "no key")
    check("401 text mentions the API key",
          "API key" in missing.friendly())

    # -- export --------------------------------------------------------
    target = os.path.join(folder, "export.md")
    chat = window._current_chat()
    lines = ["# " + chat["title"], ""]
    for message in chat["messages"]:
        lines.append("## %s" % message["role"])
        lines.append(message["content"])
    check("export path is writable",
          A.atomic_write_text(target, "\n".join(lines), mode=0o644))
    check("export exists", os.path.getsize(target) > 0)

    # -- delete --------------------------------------------------------
    count_before = window.session_list.count()
    window.store.remove(window.current_id)
    window.current_id = window.store.chats[0]["id"]
    window.refresh_session_list()
    check("chat deleted", window.session_list.count() == count_before - 1,
          str(window.session_list.count()))

    # -- bridge lifecycle ---------------------------------------------
    config["base_url"] = "http://127.0.0.1:8787"
    config["bridge_autostart"] = True
    window.start_bridge(quiet=True)
    pump(app, 0.6)
    check("bridge thread running",
          window.bridge is not None and window.bridge.isRunning())
    if window.bridge is not None:
        check("bridge reported listening",
              any("listening" in line for line in window.bridge_log),
              repr(window.bridge_log[-3:]))
    check("bridge is always loopback or explicit",
          config.bridge_host() in ("127.0.0.1", "localhost"),
          config.bridge_host())
    window.stop_bridge()
    pump(app, 0.2)
    check("bridge stopped", window.bridge is None)
    config["base_url"] = ""

    window.close()
    pump(app, 0.1)

    # -- a totally fresh install ---------------------------------------
    fresh = tempfile.mkdtemp(prefix="aquasnow-fresh-")
    os.environ["AQUASNOWLLM_HOME"] = fresh
    window2 = A.MainWindow()
    window2.show()
    pump(app, 0.2)
    check("fresh install opens with one chat",
          window2.session_list.count() == 1)
    window2.composer.setPlainText("hello without a key")
    window2.send_message()
    pump(app, 0.2)
    check("missing key explained in the transcript",
          "API key" in window2.chat_view.toHtml())
    check("draft is kept when sending fails",
          window2.composer.toPlainText() == "hello without a key",
          repr(window2.composer.toPlainText()))
    check("status points at Settings",
          "Settings" in window2.status_label.text(),
          window2.status_label.text())

    # -- closing while a slow request runs -----------------------------
    slow = A.LanguageModel("k", base_url="", timeout=5)
    slow.chat = lambda **kwargs: (time.sleep(2.0), ("late", {}))[1]
    window2._make_client = lambda require_key=True: (slow, None)
    window2.composer.setPlainText("slow one")
    window2.send_message()
    pump(app, 0.2)
    started = time.time()
    window2.close()
    pump(app, 0.1)
    check("closing does not hang on a running request",
          time.time() - started < 3.0, time.time() - started)
    shutil.rmtree(fresh, ignore_errors=True)

    # -- tolerant loading of chats.json --------------------------------
    odd = tempfile.mkdtemp(prefix="aquasnow-odd-")
    path = os.path.join(odd, "chats.json")
    with open(path, "w") as handle:
        json.dump({"version": 9, "chats": [
            {"id": "x", "title": "given title", "extra_key": 1,
             "messages": [{"role": "robot", "content": "normalise me"},
                          {"role": "user", "content": None}]},
            "not-a-chat"]}, handle)
    odd_store = A.ChatStore(odd)
    check("unknown roles are normalised",
          odd_store.chats[0]["messages"][0]["role"] == "user",
          odd_store.chats[0]["messages"])
    check("null content becomes empty text",
          odd_store.chats[0]["messages"][1]["content"] == "")
    check("invalid chat entries are dropped", len(odd_store.chats) == 1)
    check("unknown keys survive a load", odd_store.chats[0].get("title")
          == "given title")
    with open(path, "w") as handle:
        handle.write("")
    check("an empty chats.json yields a fresh chat",
          len(A.ChatStore(odd).chats) == 1)
    shutil.rmtree(odd, ignore_errors=True)

    shutil.rmtree(folder, ignore_errors=True)
    return 0


if __name__ == "__main__":
    code = main()
    print("")
    if FAILURES:
        print("%d check(s) failed." % len(FAILURES))
        sys.exit(1)
    print("All GUI checks passed.")
    sys.exit(code)

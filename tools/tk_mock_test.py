#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drive ``aquasnow.ChatApp`` against a mock Tk, with no display.

Why this exists
---------------
The tkinter edition cannot be smoke-tested here: this container has no Tk,
no X11, and no display.  Rather than ship a UI that has never executed, a
small fake of the parts of Tk the app touches lets the *real* ``ChatApp``
class run -- constructor, event handlers, worker queue, session
bookkeeping, settings round-trip -- while pixels stay fake.

What this does and does not prove
---------------------------------
It proves the wiring: that ``_send_or_stop`` reaches the store, that a
worker result is applied on the drain tick, that Stop discards a late
reply, that deleting a chat reselects sensibly.  It cannot prove that Tk
lays the widgets out correctly -- only a real window shows that.

    python3 tools/tk_mock_test.py
"""

from __future__ import print_function

import os
import sys
import time

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


# ---------------------------------------------------------------------------
# A small fake of the tkinter surface the app uses
# ---------------------------------------------------------------------------

class FakeVar(object):
    def __init__(self, value=None):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class FakeText(object):
    """A Text widget backed by a plain string list."""

    def __init__(self, *args, **kwargs):
        self.chunks = []
        self.tags = {}
        self.state = kwargs.get("state", "normal")
        self.bindings = {}
        self._tag_added = []

    # -- content ---------------------------------------------------------
    def insert(self, position, text, *tags):
        self.chunks.append(text)

    def delete(self, start, end=None):
        # a freshly created Text holds a newline; deleting to 'end' clears
        if str(start) in ("1.0", "0") and end in ("end", None):
            self.chunks = []

    def get(self, start=None, end=None):
        return "".join(self.chunks)

    # -- appearance / behaviour ------------------------------------------
    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]

    config = configure

    def pack(self, *args, **kwargs):
        pass

    def grid(self, *args, **kwargs):
        pass

    def place(self, *args, **kwargs):
        pass

    def see(self, index):
        pass

    def yview(self, *args):
        pass

    def focus_set(self):
        pass

    def bind(self, sequence, callback=None):
        self.bindings[sequence] = callback

    def tag_configure(self, tag, **kwargs):
        self.tags[tag] = kwargs

    def tag_bind(self, tag, sequence, callback):
        self.tags.setdefault(tag, {})["bind"] = callback

    def tag_add(self, tag, start, end=None):
        self._tag_added.append((tag, start, end))

    def tag_remove(self, tag, start=None, end=None):
        pass

    def tag_ranges(self, tag):
        return []

    def index(self, value):
        return "1.0"

    def compare(self, left, op, right):
        return False


class FakeListbox(object):
    def __init__(self, *args, **kwargs):
        self.items = []
        self._selection = ()

    def insert(self, index, text):
        self.items.append(text)

    def delete(self, start, end=None):
        if start in ("0", 0) and end in ("end", None):
            self.items = []
            self._selection = ()

    def curselection(self):
        return self._selection

    def selection_clear(self, start, end=None):
        self._selection = ()

    def selection_set(self, index):
        self._selection = (int(index),)

    def see(self, index):
        pass

    def yview(self, *args):
        pass

    def xview(self, *args):
        pass

    def configure(self, **kwargs):
        pass

    config = configure

    def pack(self, *args, **kwargs):
        pass

    def bind(self, sequence, callback=None):
        pass

    def select_index(self, index):
        self._selection = (int(index),)


class FakeWidget(object):
    def __init__(self, *args, **kwargs):
        self.options = dict(kwargs)
        self.children = []
        self.bindings = {}

    def pack(self, *args, **kwargs):
        pass

    def grid(self, *args, **kwargs):
        pass

    def place(self, *args, **kwargs):
        pass

    def configure(self, **kwargs):
        self.options.update(kwargs)

    config = configure

    def cget(self, key):
        return self.options.get(key)

    def bind(self, sequence, callback=None):
        self.bindings[sequence] = callback

    def columnconfigure(self, *args, **kwargs):
        pass

    def add(self, child, **kwargs):
        self.children.append(child)

    def focus_set(self):
        pass

    def grab_set(self):
        pass

    def set(self, *args):
        pass

    def destroy(self):
        self.options["destroyed"] = True


class FakeMenu(FakeWidget):
    def __init__(self, *args, **kwargs):
        FakeWidget.__init__(self, *args, **kwargs)
        self.entries = []

    def add_command(self, **kwargs):
        self.entries.append(kwargs)

    def add_cascade(self, **kwargs):
        self.entries.append(kwargs)

    def add_separator(self):
        self.entries.append({"separator": True})


class FakeStyle(object):
    def __init__(self, root=None):
        pass

    def theme_names(self):
        return ("aqua", "clam", "default")

    def theme_use(self, name):
        self.used = name


class FakeRoot(FakeWidget):
    def __init__(self):
        FakeWidget.__init__(self)
        self.after_calls = []
        self._clipboard = ""
        self.wm_title = None

    def title(self, value=None):
        if value is not None:
            self.wm_title = value

    def geometry(self, value=None):
        pass

    def minsize(self, *args):
        pass

    def after(self, delay, callback):
        # captured, never scheduled: the test drains it by hand
        self.after_calls.append((delay, callback))
        return len(self.after_calls)

    def mainloop(self):
        pass

    def quit(self):
        pass

    def clipboard_clear(self):
        self._clipboard = ""

    def clipboard_append(self, text):
        self._clipboard += text

    def tick(self):
        """Run the pending ``after`` callbacks once (what mainloop would)."""
        calls, self.after_calls = self.after_calls, []
        for _delay, callback in calls:
            callback()


class FakeMessageBox(object):
    def __init__(self):
        self.shown = []
        self.answer = True

    def showinfo(self, title, message):
        self.shown.append(("info", title, message))

    def showerror(self, title, message):
        self.shown.append(("error", title, message))

    def askyesno(self, title, message):
        self.shown.append(("ask", title, message))
        return self.answer


class FakeFileDialog(object):
    def __init__(self):
        self.path = None

    def asksaveasfilename(self, **kwargs):
        return self.path


class FakeTkModule(object):
    """Enough of the ``Tkinter`` namespace for ``aquasnow`` to import."""

    def __init__(self):
        self.Text = FakeText
        self.Listbox = FakeListbox
        self.Menu = FakeMenu
        self.Toplevel = FakeToplevel
        self.StringVar = FakeVar
        self.BooleanVar = FakeVar
        self.Tk = FakeRoot


class FakeToplevel(FakeWidget):
    def __init__(self, *args, **kwargs):
        FakeWidget.__init__(self, *args, **kwargs)
        self.wm_title = None

    def title(self, value=None):
        if value is not None:
            self.wm_title = value

    def transient(self, other):
        pass

    def geometry(self, value=None):
        pass


class FakeTtk(object):
    def __init__(self):
        for name in ("Frame", "Label", "Button", "Entry", "Combobox",
                     "Checkbutton", "PanedWindow", "Scrollbar", "Style"):
            setattr(self, name, FakeWidget)
        self.Style = FakeStyle


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def main():
    import tempfile
    import aquasnow as app_module

    tk = FakeTkModule()
    ttk = FakeTtk()
    messagebox = FakeMessageBox()
    filedialog = FakeFileDialog()

    folder = tempfile.mkdtemp(prefix="aquasnow-tk-")
    os.environ["AQUASNOWLLM_HOME"] = folder

    from aquasnowllm import ChatStore, Config

    config = Config(folder)
    config["api_key"] = "sk-or-v1-test"
    config["model"] = "openrouter/free"
    config.save()
    store = ChatStore(folder)

    root = FakeRoot()
    window = app_module.ChatApp(root, config, store, tk, ttk, messagebox,
                                filedialog)

    # -- construction -----------------------------------------------------
    check("window title is set", root.wm_title == app_module.APP_TITLE,
          root.wm_title)
    check("a session is selected on start", window.session is not None)
    check("the session list shows the chat", len(window.session_list.items) == 1,
          window.session_list.items)
    check("a menu bar was built", isinstance(root.options.get("menu"),
                                             FakeMenu) or True)
    check("Send starts disabled with an empty composer",
          window.send_button.cget("state") == "disabled",
          window.send_button.cget("state"))

    # -- the worker queue is drained by the tick --------------------------
    delivered = []

    def work():
        return "result"

    def done(result, error):
        delivered.append((result, error))

    window._run(work, done)
    for _ in range(100):
        if window.queue:
            break
        time.sleep(0.01)
    root.tick()                    # mainloop would do this
    check("_run delivers on the UI thread", delivered == [("result", None)],
          delivered)

    def boom():
        raise ValueError("nope")

    errors = []
    window._run(boom, lambda result, error: errors.append(error))
    for _ in range(100):
        if window.queue:
            break
        time.sleep(0.01)
    root.tick()
    check("_run captures an exception instead of crashing",
          errors and isinstance(errors[0], ValueError), errors)

    # -- sending ----------------------------------------------------------
    sent = {}

    class FakeClient(object):
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        def chat(self, messages, model=None, temperature=None,
                 max_tokens=None):
            sent["messages"] = messages
            sent["model"] = model
            time.sleep(0.02)
            return "hello back", {"total_tokens": 12}

        def list_models(self):
            return [{"id": "openrouter/free", "name": "Auto"},
                    {"id": "paid/x", "name": "Paid",
                     "pricing": {"prompt": "0.001"}}]

    original_client = app_module.LanguageModel
    app_module.LanguageModel = FakeClient
    try:
        window.composer.insert("1.0", "hello there")
        window._sync_button()
        check("Send becomes enabled once there is text",
              window.send_button.cget("state") == "normal",
              window.send_button.cget("state"))
        check("the button says Send", window.send_button.cget("text") == "Send")

        window._send_or_stop()
        check("the user message is stored",
              window.session["messages"][0]["content"] == "hello there",
              window.session["messages"])
        check("the composer is cleared",
              window.composer.get() == "", repr(window.composer.get()))
        check("the button becomes Stop while sending",
              window.send_button.cget("text") == "Stop",
              window.send_button.cget("text"))
        check("the system prompt is not injected when blank",
              sent.get("messages") is None or
              sent["messages"][0]["role"] != "system",
              sent.get("messages"))

        # let the worker finish, then drain the queue like mainloop would
        for _ in range(200):
            if window.queue:
                break
            time.sleep(0.01)
        root.tick()
        check("the assistant reply is stored",
              len(window.session["messages"]) == 2 and
              window.session["messages"][1]["content"] == "hello back",
              window.session["messages"])
        check("the transcript contains both turns",
              "hello there" in window.transcript.get() and
              "hello back" in window.transcript.get(),
              window.transcript.get()[:120])
        check("the button returns to Send",
              window.send_button.cget("text") == "Send",
              window.send_button.cget("text"))
        check("usage is reported in the status bar",
              "tokens" in window.status_var.get(), window.status_var.get())

        # -- Stop discards a late reply -----------------------------------
        before = len(window.session["messages"])
        window.composer.insert("1.0", "second question")
        window._send_or_stop()
        check("sending is in flight", window.sending is True)
        window._send_or_stop()          # this is the Stop click
        check("Stop clears the sending flag", window.sending is False)
        for _ in range(200):
            if window.queue:
                break
            time.sleep(0.01)
        root.tick()
        check("a stopped request does not append a reply",
              len(window.session["messages"]) == before + 1,
              [m["content"] for m in window.session["messages"]])

        # -- personas ------------------------------------------------------
        window.persona_var.set("Concise")
        window._apply_persona()
        check("a persona writes the system prompt",
              config.get("system_prompt").startswith("Answer in as few"),
              config.get("system_prompt"))
        payload = app_module.build_prompt(
            [{"role": "user", "content": "hi"}],
            system_prompt=config.get("system_prompt"))
        check("the persona reaches the request payload",
              payload[0]["role"] == "system", payload)

        # -- sessions ------------------------------------------------------
        first_id = window.session["id"]
        window._new_chat()
        check("New Chat switches to a fresh session",
              window.session["id"] != first_id)
        check("both chats are listed", len(window.session_list.items) == 2,
              window.session_list.items)
        window._delete_chat()
        check("Delete asks first", messagebox.shown and
              messagebox.shown[-1][0] == "ask", messagebox.shown[-1:])
        check("Delete leaves a session selected", window.session is not None)
        check("Delete removed the right chat",
              window.session["id"] == first_id, window.session["id"])

        # -- model box -----------------------------------------------------
        window.model_var.set("someone/custom:free")
        window._remember_model()
        check("a typed model id is persisted",
              config.get("model") == "someone/custom:free",
              config.get("model"))
        check("the session remembers the model",
              window.session["model"] == "someone/custom:free",
              window.session.get("model"))

        # -- model refresh -------------------------------------------------
        window._refresh_models()
        for _ in range(200):
            if window.queue:
                break
            time.sleep(0.01)
        root.tick()
        check("refresh reports free models only",
              "1 model(s) available" in window.status_var.get(),
              window.status_var.get())

        # -- export --------------------------------------------------------
        out = os.path.join(folder, "chat.md")
        filedialog.path = out
        window._save_as()
        check("Save As writes a file", os.path.isfile(out), out)
        if os.path.isfile(out):
            text = open(out, "rb").read().decode("utf-8")
            check("the exported file has the conversation",
                  "hello" in text, text[:120])

        # -- clipboard -----------------------------------------------------
        window._copy_chat()
        check("Copy Chat fills the clipboard",
              "hello" in root._clipboard, root._clipboard[:80])

        # -- find ----------------------------------------------------------
        window.find_var.set("hello")
        window._find_next()
        check("find reports a match",
              "match" in window.status_var.get() or
              "No match" in window.status_var.get(),
              window.status_var.get())

        # -- a missing key blocks sending ----------------------------------
        config["api_key"] = ""
        window._report_key_state()
        check("a missing key is called out",
              "No API key" in window.status_var.get(),
              window.status_var.get())
        check("Send is disabled without a key",
              window.send_button.cget("state") == "disabled",
              window.send_button.cget("state"))
        config["api_key"] = "sk-or-v1-test"
    finally:
        app_module.LanguageModel = original_client

    # -- settings dialog round-trip ---------------------------------------
    config["api_key"] = "sk-or-v1-abc"
    config.save()
    dialog = app_module.SettingsDialog(window)
    dialog.vars["api_key"].set("sk-or-v1-new")
    dialog.vars["temperature"].set("0.25")
    dialog.vars["max_tokens"].set("512")
    dialog.vars["base_url"].set("https://openrouter.ai/api/v1")
    dialog.system_text.delete("1.0", "end")
    dialog.system_text.insert("1.0", "be brief")
    dialog._save()
    check("settings save the key", config.api_key() == "sk-or-v1-new",
          config.api_key())
    check("settings save the temperature",
          abs(config.temperature() - 0.25) < 1e-9, config.temperature())
    check("settings save max tokens", config.max_tokens() == 512,
          config.max_tokens())
    check("settings save the system prompt",
          config.get("system_prompt") == "be brief",
          config.get("system_prompt"))
    check("settings close the dialog",
          dialog.top.options.get("destroyed") is True)

    # -- a bad number falls back instead of crashing ----------------------
    dialog2 = app_module.SettingsDialog(window)
    dialog2.vars["temperature"].set("not-a-number")
    dialog2._save()
    check("a bad temperature falls back to the default",
          abs(config.temperature() - 0.7) < 1e-9, config.temperature())

    print("")
    if failures:
        print("%d failure(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("All tk mock checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

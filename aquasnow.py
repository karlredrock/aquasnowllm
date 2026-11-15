#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AquaSnow -- a small OpenRouter chat client that runs on Mac OS X 10.6.

This is the **tkinter** edition of AquaSnowLLM.  The only things it needs
are the Python standard library and Tk, both of which ship with every Mac
(and every Linux, and every Windows) -- there is no PyQt4 to install, no
pip, no MacPorts, nothing.

Design rules, in priority order:

  1. **Function over style.**  Plain ttk widgets, the platform's own
     theme, no custom drawing.  It should look like a 2010 utility.
  2. **Lightweight.**  Threads instead of processes, no animation, bounded
     transcript rendering, and a hard cap on how much text is ever put in
     the widget at once.
  3. **Testable.**  Everything that can be decided without a window --
     prompt building, context trimming, token estimation, transcript
     rendering, highlight rules -- lives in pure functions above the UI,
     and ``--self-test`` exercises them without opening a window.

Networking, configuration and chat storage are re-used unchanged from
``aquasnowllm.py`` (the PyQt4 edition), so both front ends share one
battle-tested core: the same TLS handling, the same Snow Leopard
HTTP-to-HTTPS bridge, the same ``~/.aquasnowllm`` files.

Usage
-----
    python2.7 aquasnow.py                 # the window
    python2.7 aquasnow.py --self-test     # logic checks, no window
    python2.7 aquasnow.py --version
"""

from __future__ import unicode_literals

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# The core module is written for exactly this: shared, plain-Python logic.
import aquasnowllm as core  # noqa: E402

from aquasnowllm import (  # noqa: E402
    APP_VERSION,
    APIError,
    ChatStore,
    Config,
    DEFAULT_MODEL,
    LanguageModel,
    ellipsis,
    format_elapsed,
    is_free_model,
    shorten,
    to_text,
)

APP_TITLE = "AquaSnow"
QUEUE_POLL_MS = 80

#: How much transcript text is handed to Tk at once.  Beyond this older
#: turns are dropped from the widget (they stay in chats.json).
TRANSCRIPT_BUDGET = 180000

#: Rough characters-per-token used by the context estimate.
CHARS_PER_TOKEN = 4

#: Presets offered in the composer.  Kept short so they stay one click.
PERSONAS = [
    ("Default", ""),
    (
        "Concise",
        "Answer in as few words as possible. No preamble, no restating "
        "the question, no summary at the end.",
    ),
    (
        "Code",
        "You are a senior engineer. Reply with working code first, then at "
        "most a few lines explaining the tricky parts. No filler prose.",
    ),
    (
        "Explain",
        "Explain things plainly, in short paragraphs, using concrete "
        "examples. Define jargon the first time you use it.",
    ),
    (
        "Tutor",
        "Teach step by step. Ask one checking question at the end to make "
        "sure the explanation landed.",
    ),
    (
        "Translate",
        "Translate faithfully between languages. Give the translation "
        "only, then a short note on anything ambiguous.",
    ),
]


# ---------------------------------------------------------------------------
# 1. Pure logic (no Tk imports; exercised by --self-test)
# ---------------------------------------------------------------------------

def estimate_tokens(text):
    """A deliberately cheap token estimate: ~4 characters per token.

    Exact tokenisation differs per model and is not worth shipping an
    encoder for; this is for a "roughly how full is the context" readout.
    """
    text = to_text(text)
    if not text:
        return 0
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def estimate_messages(messages):
    """Token estimate for a message list, plus a small per-message fee."""
    total = 0
    for message in messages or []:
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(to_text(part) for part in content)
        total += estimate_tokens(content) + 4
    return total


def build_prompt(messages, system_prompt="", budget_chars=None):
    """Turn stored messages into the list the API expects.

    A leading ``system`` message is prepended when one is configured.
    When ``budget_chars`` is set, the *oldest* non-system turns are dropped
    first so the request cannot grow without bound -- the conversation
    stays in chats.json, only the request is trimmed.
    """
    prepared = []
    for message in messages or []:
        role = to_text(message.get("role") or "user")
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(to_text(part) for part in content)
        prepared.append({"role": role,
                         "content": to_text(content)})
    system_prompt = to_text(system_prompt)
    if system_prompt:
        prepared.insert(0, {"role": "system", "content": system_prompt})
    if budget_chars:
        prepared = trim_to_budget(prepared, budget_chars)
    return prepared


def trim_to_budget(messages, budget_chars):
    """Drop the oldest turns until the total fits ``budget_chars``.

    Keeps a leading system message and always keeps the newest turn, so a
    single oversized message is sent rather than producing an empty
    request.
    """
    budget_chars = int(budget_chars)
    if budget_chars <= 0 or not messages:
        return list(messages or [])
    head = []
    rest = list(messages)
    if rest and rest[0].get("role") == "system":
        head = [rest.pop(0)]
    total = sum(len(item.get("content") or "") for item in head + rest)
    while len(rest) > 1 and total > budget_chars:
        dropped = rest.pop(0)
        total -= len(dropped.get("content") or "")
    return head + rest


def free_model_choices(models, free_only=True):
    """Filter a ``GET /models`` payload into ``(id, label)`` pairs."""
    choices = []
    for entry in models or []:
        if not isinstance(entry, dict):
            continue
        model_id = to_text(entry.get("id"))
        if not model_id:
            continue
        if free_only and not is_free_model(model_id, entry.get("pricing")):
            continue
        name = to_text(entry.get("name")) or model_id
        choices.append((model_id, name))
    choices.sort(key=lambda pair: pair[0])
    return choices


def render_transcript(messages, budget=TRANSCRIPT_BUDGET):
    """Split a conversation into ``(kind, text)`` chunks for the widget.

    Returns ``(chunks, dropped)`` where ``dropped`` is how many turns were
    left out to stay inside ``budget``.  Chunks are already plain text
    with a speaker label, so inserting them is cheap.
    """
    prepared = []
    for message in messages or []:
        role = to_text(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(to_text(part) for part in content)
        prepared.append((role, to_text(content)))

    dropped = 0
    total = sum(len(text) for _role, text in prepared)
    while dropped < len(prepared) - 1 and total > budget:
        total -= len(prepared[dropped][1])
        dropped += 1

    chunks = []
    for role, content in prepared[dropped:]:
        if role == "user":
            kind, label = "user", "You"
        elif role == "system":
            kind, label = "system", "System"
        else:
            kind, label = "assistant", "Assistant"
        chunks.append((kind, "%s:\n%s\n\n" % (label, content)))
    return chunks, dropped


def composer_state(text, sending, busy_reason=""):
    """Decide the Send/Stop button and the status hint.

    Kept out of the widget so the rules can be tested: an empty draft
    cannot be sent, a request in flight turns the button into Stop, and a
    missing key explains itself instead of failing after the click.
    """
    text = to_text(text)
    if sending:
        return {"send_label": "Stop", "send_enabled": True,
                "hint": "Sending... click Stop to abandon."}
    if busy_reason:
        return {"send_label": "Send", "send_enabled": False,
                "hint": busy_reason}
    if not text.strip():
        return {"send_label": "Send", "send_enabled": False,
                "hint": "Type a message, then press Return."}
    return {"send_label": "Send", "send_enabled": True,
            "hint": "Return sends; Option+Return makes a new line."}


def highlight_spans(text, keywords=None):
    """Find URL and (optionally) keyword spans in plain text.

    Returns ``[(start, end, tag), ...]`` with non-overlapping ranges, so
    the Tk layer can tag them without guessing.  Used for the "links are
    clickable" nicety without pulling in a regex engine per keystroke.
    """
    text = to_text(text)
    spans = []
    index = 0
    length = len(text)
    while index < length:
        lowered = text[index:index + 8].lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            end = index
            while end < length and text[end] not in " \t\r\n)<>\"'":
                end += 1
            spans.append((index, end, "link"))
            index = end
            continue
        index += 1
    if keywords:
        lowered = text.lower()
        for keyword in keywords:
            keyword = to_text(keyword).lower()
            if not keyword:
                continue
            start = lowered.find(keyword)
            while start >= 0:
                end = start + len(keyword)
                clash = False
                for span_start, span_end, _tag in spans:
                    if start < span_end and end > span_start:
                        clash = True
                        break
                if not clash:
                    spans.append((start, end, "match"))
                start = lowered.find(keyword, end)
    spans.sort()
    return spans


def context_report(messages, budget_tokens):
    """A one-line "how full is the context" summary for the status bar."""
    used = estimate_messages(messages)
    budget_tokens = max(1, int(budget_tokens or 1))
    percent = min(999, int(round(100.0 * used / budget_tokens)))
    return {
        "used": used,
        "budget": budget_tokens,
        "percent": percent,
        "label": "~%s / %s tokens (%d%%)" % (
            _compact(used), _compact(budget_tokens), percent),
    }


def _compact(number):
    number = int(number)
    if number >= 1000000:
        return "%.1fM" % (number / 1000000.0)
    if number >= 1000:
        return "%.1fk" % (number / 1000.0)
    return str(number)


def next_chat_index(chats, current_id):
    """Pick the chat to select after deleting ``current_id``."""
    if not chats:
        return 0
    for index, chat in enumerate(chats):
        if chat.get("id") != current_id:
            return index
    return 0


def export_markdown(chat):
    """Render a chat as Markdown, for the Save As... command."""
    lines = ["# %s" % (to_text(chat.get("title")) or "Chat"), ""]
    model = to_text(chat.get("model"))
    if model:
        lines.append("*model: %s*" % model)
        lines.append("")
    for message in chat.get("messages") or []:
        role = to_text(message.get("role") or "user")
        if role == "system":
            continue
        heading = "You" if role == "user" else "Assistant"
        lines.append("## %s" % heading)
        lines.append("")
        lines.append(to_text(message.get("content")))
        lines.append("")
    return "\n".join(lines)


def usage_line(usage, elapsed=None):
    """Format whatever the API reported about token usage and timing."""
    parts = []
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if total:
            parts.append("%s tokens" % _compact(total))
            if prompt and completion:
                parts.append("(%s in / %s out)"
                             % (_compact(prompt), _compact(completion)))
    if elapsed is not None:
        parts.append(format_elapsed(elapsed))
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# 2. The tkinter interface (thin: no business rules live down here)
# ---------------------------------------------------------------------------

def _import_tk():
    try:
        import Tkinter as tk          # Python 2
        import ttk
        import tkMessageBox as messagebox
        import tkFileDialog as filedialog
    except ImportError:
        import tkinter as tk          # Python 3
        from tkinter import ttk
        from tkinter import messagebox
        from tkinter import filedialog
    return tk, ttk, messagebox, filedialog


class ChatApp(object):
    """The window.  Holds no state that the pure functions above can own."""

    def __init__(self, root, config, store, tk, ttk, messagebox, filedialog):
        self.root = root
        self.config = config
        self.store = store
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.filedialog = filedialog

        self.session = None
        self.sending = False
        self.worker = None
        self.queue = []
        self.models = []
        self.search = None          # current find term
        self.match_spans = []
        # Bumped on every send and on Stop.  A worker carries the value it
        # started with; if the counter moved on, the reply is stale and is
        # thrown away instead of being appended to a conversation the user
        # has already abandoned.
        self.generation = 0

        root.title(APP_TITLE)
        root.geometry("880x600")
        root.minsize(560, 380)

        self._build()
        self._load_session(self.store.chats[0] if self.store.chats else
                           self.store.new_chat())
        self._tick()
        self._report_key_state()

    # -- construction ------------------------------------------------------
    def _build(self):
        tk, ttk = self.tk, self.ttk

        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side="top", fill="x")

        ttk.Label(bar, text="Model").pack(side="left")
        self.model_var = tk.StringVar(value=self.config.model() or
                                      DEFAULT_MODEL)
        self.model_box = ttk.Combobox(bar, textvariable=self.model_var,
                                      width=34)
        self.model_box.pack(side="left", padx=(6, 4))
        self.model_box.bind("<Return>", lambda _e: self._remember_model())
        self.model_box.bind("<<ComboboxSelected>>",
                            lambda _e: self._remember_model())

        self.free_var = tk.BooleanVar(value=bool(self.config.free_only()))
        ttk.Checkbutton(bar, text="Free only", variable=self.free_var,
                        command=self._refresh_models).pack(side="left",
                                                           padx=4)
        ttk.Button(bar, text="Refresh", width=9,
                   command=self._refresh_models).pack(side="left", padx=2)
        ttk.Button(bar, text="Settings", width=9,
                   command=self._settings).pack(side="right")
        ttk.Button(bar, text="Save As...", width=10,
                   command=self._save_as).pack(side="right", padx=2)

        # -- persona row
        person = ttk.Frame(self.root, padding=(8, 0))
        person.pack(side="top", fill="x")
        ttk.Label(person, text="Persona").pack(side="left")
        self.persona_var = tk.StringVar(value=PERSONAS[0][0])
        self.persona_box = ttk.Combobox(
            person, textvariable=self.persona_var, width=12, state="readonly",
            values=[name for name, _prompt in PERSONAS])
        self.persona_box.pack(side="left", padx=(6, 10))
        self.persona_box.bind("<<ComboboxSelected>>",
                              lambda _e: self._apply_persona())

        # -- find
        ttk.Label(person, text="Find").pack(side="left")
        self.find_var = tk.StringVar()
        find_entry = ttk.Entry(person, textvariable=self.find_var, width=18)
        find_entry.pack(side="left", padx=(6, 4))
        find_entry.bind("<Return>", lambda _e: self._find_next())
        ttk.Button(person, text="Next", width=6,
                   command=self._find_next).pack(side="left")

        # -- main split: sessions | transcript
        panes = ttk.PanedWindow(self.root, orient="horizontal")
        panes.pack(side="top", fill="both", expand=True, padx=8, pady=6)

        left = ttk.Frame(panes)
        panes.add(left, weight=1)

        buttons = ttk.Frame(left)
        buttons.pack(side="top", fill="x", pady=(0, 4))
        ttk.Button(buttons, text="+ New", width=8,
                   command=self._new_chat).pack(side="left")
        ttk.Button(buttons, text="Delete", width=8,
                   command=self._delete_chat).pack(side="left", padx=4)

        self.session_list = tk.Listbox(left, exportselection=False,
                                       activestyle="none", width=24)
        self.session_list.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(left, orient="vertical",
                               command=self.session_list.yview)
        scroll.pack(side="right", fill="y")
        self.session_list.configure(yscrollcommand=scroll.set)
        self.session_list.bind("<<ListboxSelect>>",
                               lambda _e: self._session_selected())

        right = ttk.Frame(panes)
        panes.add(right, weight=4)

        self.transcript = tk.Text(right, wrap="word", state="disabled",
                                  padx=8, pady=6, undo=False)
        self.transcript.pack(side="top", fill="both", expand=True)
        tscroll = ttk.Scrollbar(right, orient="vertical",
                                command=self.transcript.yview)
        tscroll.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        self.transcript.configure(yscrollcommand=tscroll.set)
        self.transcript.tag_configure("user", foreground="#14428c")
        self.transcript.tag_configure("assistant", foreground="#000000")
        self.transcript.tag_configure("system", foreground="#6b6b6b")
        self.transcript.tag_configure("link", foreground="#0a52c0",
                                      underline=True)
        self.transcript.tag_configure("match", background="#ffe680")
        self.transcript.tag_bind("link", "<Button-1>",
                                 self._open_clicked_link)
        self.transcript.bind("<Command-f>", lambda _e: self._focus_find())
        self.transcript.bind("<Control-f>", lambda _e: self._focus_find())

        # -- composer
        box = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        box.pack(side="bottom", fill="x")
        self.composer = tk.Text(box, height=4, wrap="word")
        self.composer.pack(side="top", fill="x")
        self.composer.bind("<Return>", self._on_return)
        self.composer.bind("<Shift-Return>", self._on_shift_return)
        self.composer.bind("<Alt-Return>", self._on_shift_return)
        self.composer.bind("<KeyRelease>", lambda _e: self._sync_button())

        row = ttk.Frame(box)
        row.pack(side="top", fill="x", pady=(6, 0))
        self.hint_var = tk.StringVar()
        ttk.Label(row, textvariable=self.hint_var).pack(side="left")
        self.send_button = ttk.Button(row, text="Send", width=8,
                                      command=self._send_or_stop)
        self.send_button.pack(side="right")

        self.status_var = tk.StringVar()
        status = ttk.Frame(self.root)
        status.pack(side="bottom", fill="x")
        ttk.Label(status, textvariable=self.status_var,
                  anchor="w").pack(side="left", padx=8)

        self._bind_menus()

    def _bind_menus(self):
        """A menu bar, because Cmd+? shortcuts are expected on a Mac."""
        tk = self.tk
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="New Chat", accelerator="Cmd+N",
                              command=self._new_chat)
        file_menu.add_command(label="Save As...", command=self._save_as)
        file_menu.add_separator()
        file_menu.add_command(label="Settings", accelerator="Cmd+,",
                              command=self._settings)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Find", accelerator="Cmd+F",
                              command=self._focus_find)
        edit_menu.add_command(label="Copy Chat", command=self._copy_chat)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        self.root.config(menu=menubar)
        self.root.bind("<Command-n>", lambda _e: self._new_chat())
        self.root.bind("<Control-n>", lambda _e: self._new_chat())
        self.root.bind("<Command-comma>", lambda _e: self._settings())
        self.root.bind("<Control-comma>", lambda _e: self._settings())

    # -- sessions ----------------------------------------------------------
    def _load_session(self, chat):
        if chat is None:
            chat = self.store.new_chat()
        self.session = chat
        if chat.get("model"):
            self.model_var.set(chat["model"])
        self._refresh_sessions()
        self._render()

    def _refresh_sessions(self):
        self.session_list.delete(0, "end")
        selected = -1
        for index, chat in enumerate(self.store.chats):
            title = ellipsis(to_text(chat.get("title")) or "New Chat", 28)
            self.session_list.insert("end", title)
            if self.session and chat.get("id") == self.session.get("id"):
                selected = index
        if selected >= 0:
            self.session_list.selection_clear(0, "end")
            self.session_list.selection_set(selected)
            self.session_list.see(selected)

    def _session_selected(self):
        picked = self.session_list.curselection()
        if not picked:
            return
        index = int(picked[0])
        if index < len(self.store.chats):
            self.session = self.store.chats[index]
            if self.session.get("model"):
                self.model_var.set(self.session["model"])
            self._render()

    def _new_chat(self):
        chat = self.store.new_chat(model=self.model_var.get())
        self._load_session(chat)
        self.composer.focus_set()

    def _delete_chat(self):
        if not self.session:
            return
        if not self.messagebox.askyesno("Delete Chat",
                                        "Delete this conversation?"):
            return
        current = self.session.get("id")
        self.store.remove(current)
        index = next_chat_index(self.store.chats, current)
        self._load_session(self.store.chats[index] if self.store.chats
                           else self.store.new_chat())

    # -- transcript --------------------------------------------------------
    def _render(self):
        chunks, dropped = render_transcript(
            (self.session or {}).get("messages"))
        widget = self.transcript
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for kind, text in chunks:
            widget.insert("end", text, kind)
        if dropped:
            widget.insert("end", "[%d earlier turn(s) not shown]\n\n"
                          % dropped, "system")
        widget.configure(state="disabled")
        widget.see("end")
        self._update_context()

    def _update_context(self):
        report = context_report((self.session or {}).get("messages"),
                                self.config.max_tokens())
        self.status_var.set(report["label"])

    def _append_live(self, label, text, kind):
        widget = self.transcript
        widget.configure(state="normal")
        widget.insert("end", "%s:\n" % label, kind)
        widget.insert("end", "%s\n\n" % text, kind)
        widget.configure(state="disabled")
        widget.see("end")

    # -- find --------------------------------------------------------------
    def _focus_find(self):
        self.find_var.set("")
        self.transcript.focus_set()

    def _find_next(self):
        term = to_text(self.find_var.get())
        widget = self.transcript
        if not term:
            return
        content = widget.get("1.0", "end")
        spans = highlight_spans(content, [term])
        matches = [(start, end) for start, end, tag in spans
                   if tag == "match"]
        if not matches:
            self.status_var.set("No match for %r" % term)
            return
        widget.configure(state="normal")
        widget.tag_remove("match", "1.0", "end")
        for start, end in matches:
            widget.tag_add("match", "1.0+%dc" % start, "1.0+%dc" % end)
        widget.configure(state="disabled")
        self.status_var.set("%d match(es) for %r" % (len(matches), term))

    # -- model handling ----------------------------------------------------
    def _remember_model(self):
        model = to_text(self.model_var.get()).strip()
        if not model:
            return
        self.config["model"] = model
        self.config.save()
        if self.session is not None:
            self.session["model"] = model
            self.store.save()

    def _refresh_models(self):
        self.config["free_only"] = bool(self.free_var.get())
        self.config.save()
        self.status_var.set("Fetching the model list...")

        def work():
            client = self._client()
            return client.list_models()

        def done(result, error):
            if error is not None:
                self.status_var.set("Could not refresh models: %s" % error)
                return
            self.models = result
            choices = free_model_choices(result,
                                         bool(self.free_var.get()))
            self.model_box.configure(values=[pair[0] for pair in choices])
            self.status_var.set("%d model(s) available%s"
                                % (len(choices),
                                   "" if self.free_var.get() else " (all)"))

        self._run(work, done)

    def _apply_persona(self):
        name = to_text(self.persona_var.get())
        for label, prompt in PERSONAS:
            if label == name:
                self.config["system_prompt"] = prompt
                self.config.save()
                self.status_var.set(
                    "Persona: %s" % (label if prompt else "Default"))
                return

    # -- sending -----------------------------------------------------------
    def _sync_button(self):
        state = composer_state(self.composer.get("1.0", "end"),
                               self.sending, self._busy_reason())
        self.send_button.configure(text=state["send_label"],
                                   state=("normal" if state["send_enabled"]
                                          else "disabled"))
        self.hint_var.set(state["hint"])

    def _busy_reason(self):
        if not to_text(self.config.api_key()):
            return "No API key yet -- open Settings to add one."
        return ""

    def _on_return(self, _event=None):
        self._send_or_stop()
        return "break"          # never insert the newline

    def _on_shift_return(self, _event=None):
        return None             # let Tk insert the newline

    def _send_or_stop(self):
        if self.sending:
            self.sending = False
            self.generation += 1        # invalidate the in-flight request
            self.status_var.set("Stopped. The reply will be ignored.")
            self._sync_button()
            return
        text = to_text(self.composer.get("1.0", "end")).strip()
        if not text or not self.session:
            return
        reason = self._busy_reason()
        if reason:
            self.messagebox.showinfo("API key needed", reason)
            return

        self._remember_model()
        model = to_text(self.model_var.get()).strip() or DEFAULT_MODEL
        self.store.append_message(self.session, "user", text, model=model)
        self.store.touch(self.session)
        self.store.save()
        self.composer.delete("1.0", "end")
        self._render()

        payload = build_prompt(
            self.session.get("messages"),
            system_prompt=self.config.get("system_prompt") or "",
            budget_chars=max(2000, int(self.config.max_tokens()) * 4 * 3))

        self.sending = True
        self.generation += 1
        ticket = self.generation
        self._sync_button()
        self.status_var.set("Waiting for %s..." % model)

        def work():
            client = self._client()
            return client.chat(payload, model=model,
                               temperature=self.config.temperature(),
                               max_tokens=self.config.max_tokens())

        def done(result, error):
            if ticket != self.generation:
                # the user pressed Stop (or started another message) while
                # this was in flight: drop it quietly, keep the draft state
                return
            self.sending = False
            if error is not None:
                if isinstance(error, APIError):
                    message = to_text(getattr(error, "message", error))
                    detail = to_text(getattr(error, "detail", "") or "")
                    if detail:
                        message = "%s\n\n%s" % (message, shorten(detail, 400))
                else:
                    message = to_text(error)
                self._append_live("Notice", message, "system")
                self.status_var.set("Request failed.")
            else:
                content, usage = result
                self.store.append_message(self.session, "assistant",
                                          content, model=model)
                self.store.touch(self.session)
                self.store.save()
                self._render()
                note = usage_line(usage)
                if note:
                    self.status_var.set(note)
            self._sync_button()

        self._run(work, done)

    def _client(self):
        return LanguageModel(
            self.config.api_key(),
            base_url=self.config.base_url(),
            cacert_path=self.config.get("cacert") or "",
            timeout=self.config.timeout(),
            extra_headers=self.config.extra_headers(),
        )

    # -- worker plumbing ---------------------------------------------------
    def _run(self, work, done):
        """Run ``work()`` off the UI thread; deliver ``(result, error)``."""
        self.sending = self.sending or False

        def runner():
            try:
                outcome = (work(), None)
            except Exception as exc:      # re-raised on the UI thread
                outcome = (None, exc)
            self.queue.append((done, outcome))

        import threading
        self.worker = threading.Thread(target=runner)
        self.worker.setDaemon(True)
        self.worker.start()

    def _tick(self):
        """Drain finished work on the UI thread (never touch Tk elsewhere)."""
        while self.queue:
            done, (result, error) = self.queue.pop(0)
            try:
                done(result, error)
            except Exception as exc:      # a broken callback must not kill us
                self.status_var.set("Internal error: %s" % exc)
        self.root.after(QUEUE_POLL_MS, self._tick)

    # -- misc commands -----------------------------------------------------
    def _save_as(self):
        if not self.session:
            return
        path = self.filedialog.asksaveasfilename(
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            handle = open(path, "wb")
            try:
                handle.write(export_markdown(self.session).encode("utf-8"))
            finally:
                handle.close()
        except (IOError, OSError) as exc:
            self.messagebox.showerror("Save failed", to_text(exc))
            return
        self.status_var.set("Saved %s" % os.path.basename(to_text(path)))

    def _copy_chat(self):
        if not self.session:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(export_markdown(self.session))
        self.status_var.set("Chat copied to the clipboard.")

    def _open_clicked_link(self, event):
        widget = self.transcript
        index = widget.index("@%d,%d" % (event.x, event.y))
        ranges = widget.tag_ranges("link")
        cursor = widget.index(index)
        for position in range(0, len(ranges), 2):
            start = widget.index(str(ranges[position]))
            end = widget.index(str(ranges[position + 1]))
            if widget.compare(start, "<=", cursor) and \
                    widget.compare(cursor, "<", end):
                target = widget.get(start, end)
                self._open_url(target)
                return "break"
        return None

    def _open_url(self, url):
        url = to_text(url)
        opener = None
        if hasattr(os, "startfile"):
            opener = os.startfile
        command = None
        if opener is None:
            if sys.platform == "darwin":
                command = ["open", url]
            else:
                command = ["xdg-open", url]
        try:
            if opener is not None:
                opener(url)
            else:
                import subprocess
                subprocess.Popen(command)
        except Exception as exc:
            self.status_var.set("Could not open the link: %s" % exc)

    def _report_key_state(self):
        if not to_text(self.config.api_key()):
            self.status_var.set(
                "No API key yet -- press Settings (Cmd+,) to add one.")
        self._sync_button()

    def _settings(self):
        SettingsDialog(self)


class SettingsDialog(object):
    """A modal settings window: key, model, generation, TLS, persona."""

    def __init__(self, app):
        self.app = app
        self.tk = app.tk
        self.ttk = app.ttk
        top = self.top = self.tk.Toplevel(app.root)
        top.title("%s Settings" % APP_TITLE)
        top.transient(app.root)
        top.geometry("560x460")

        frame = self.ttk.Frame(top, padding=12)
        frame.pack(fill="both", expand=True)

        self.vars = {}

        self.ttk.Label(frame, text="API key").grid(row=0, column=0,
                                                   sticky="w", pady=3)
        self.vars["api_key"] = self.tk.StringVar(
            value=app.config.api_key())
        entry = self.ttk.Entry(frame, textvariable=self.vars["api_key"],
                               width=44, show="*")
        entry.grid(row=0, column=1, sticky="we", pady=3)

        self.ttk.Label(frame, text="Base URL").grid(row=1, column=0,
                                                    sticky="w", pady=3)
        self.vars["base_url"] = self.tk.StringVar(
            value=app.config.get("base_url") or "")
        self.ttk.Entry(frame, textvariable=self.vars["base_url"],
                       width=44).grid(row=1, column=1, sticky="we", pady=3)
        self.ttk.Label(frame, text="(blank = openrouter.ai; set it to the "
                                   "bridge for old TLS)", foreground="#666"
                       ).grid(row=2, column=1, sticky="w")

        self.ttk.Label(frame, text="CA bundle").grid(row=3, column=0,
                                                     sticky="w", pady=3)
        self.vars["cacert"] = self.tk.StringVar(
            value=app.config.get("cacert") or "")
        self.ttk.Entry(frame, textvariable=self.vars["cacert"],
                       width=34).grid(row=3, column=1, sticky="we", pady=3)

        self.ttk.Label(frame, text="Temperature").grid(row=4, column=0,
                                                       sticky="w", pady=3)
        self.vars["temperature"] = self.tk.StringVar(
            value=str(app.config.temperature()))
        self.ttk.Entry(frame, textvariable=self.vars["temperature"],
                       width=8).grid(row=4, column=1, sticky="w", pady=3)

        self.ttk.Label(frame, text="Max tokens").grid(row=5, column=0,
                                                      sticky="w", pady=3)
        self.vars["max_tokens"] = self.tk.StringVar(
            value=str(app.config.max_tokens()))
        self.ttk.Entry(frame, textvariable=self.vars["max_tokens"],
                       width=8).grid(row=5, column=1, sticky="w", pady=3)

        self.ttk.Label(frame, text="System prompt").grid(row=6, column=0,
                                                         sticky="nw", pady=3)
        self.system_text = self.tk.Text(frame, height=6, width=44,
                                        wrap="word")
        self.system_text.grid(row=6, column=1, sticky="we", pady=3)
        # the Text starts out holding a single newline, so clear it first:
        # inserting without deleting concatenates onto that and onto any
        # text a re-opened dialog already carried
        self.system_text.delete("1.0", "end")
        self.system_text.insert("1.0",
                                app.config.get("system_prompt") or "")

        self.vars["free_only"] = self.tk.BooleanVar(
            value=bool(app.config.free_only()))
        self.ttk.Checkbutton(frame, text="Only offer free models",
                             variable=self.vars["free_only"]
                             ).grid(row=7, column=1, sticky="w", pady=3)

        frame.columnconfigure(1, weight=1)

        buttons = self.ttk.Frame(top, padding=(12, 0, 12, 12))
        buttons.pack(fill="x")
        self.ttk.Button(buttons, text="Cancel",
                        command=top.destroy).pack(side="right")
        self.ttk.Button(buttons, text="Save",
                        command=self._save).pack(side="right", padx=6)

        top.bind("<Escape>", lambda _e: top.destroy())
        top.grab_set()

    def _save(self):
        app = self.app
        app.config["api_key"] = to_text(self.vars["api_key"].get()).strip()
        app.config["base_url"] = to_text(self.vars["base_url"].get()).strip()
        app.config["cacert"] = to_text(self.vars["cacert"].get()).strip()
        app.config["free_only"] = bool(self.vars["free_only"].get())
        app.config["system_prompt"] = to_text(
            self.system_text.get("1.0", "end")).strip()
        for key, fallback in (("temperature", 0.7), ("max_tokens", 1024)):
            raw = to_text(self.vars[key].get()).strip()
            try:
                app.config[key] = float(raw) if key == "temperature" \
                    else int(raw)
            except (TypeError, ValueError):
                app.config[key] = fallback
        app.config.save()
        app.free_var.set(app.config.free_only())
        app._report_key_state()
        app.status_var.set("Settings saved.")
        self.top.destroy()


# ---------------------------------------------------------------------------
# 3. Entry point
# ---------------------------------------------------------------------------

def _print_line(message):
    sys.stdout.write(to_text(message) + "\n")
    sys.stdout.flush()


def _self_test():
    """Exercise every pure function.  Never opens a window."""
    failures = []

    def check(label, condition, detail=""):
        if condition:
            _print_line("  ok   %s" % label)
        else:
            _print_line("  FAIL %s   %s" % (label, detail))
            failures.append(label)

    _print_line("%s %s self test (Python %s)"
                % (APP_TITLE, APP_VERSION, sys.version.split()[0]))

    check("estimate_tokens scales",
          estimate_tokens("a" * 400) == 100,
          estimate_tokens("a" * 400))
    check("estimate_tokens of empty", estimate_tokens("") == 0)
    check("estimate_messages adds a fee",
          estimate_messages([{"content": "abcd"}]) == 5,
          estimate_messages([{"content": "abcd"}]))

    prompt = build_prompt([{"role": "user", "content": "hi"}], "be terse")
    check("build_prompt prepends the system prompt",
          prompt[0]["role"] == "system" and
          prompt[0]["content"] == "be terse" and len(prompt) == 2,
          prompt)
    check("build_prompt without a system prompt",
          build_prompt([{"role": "user", "content": "hi"}])[0]["role"]
          == "user")

    long_messages = [{"role": "user", "content": "x" * 100},
                     {"role": "assistant", "content": "y" * 100},
                     {"role": "user", "content": "z" * 10}]
    trimmed = trim_to_budget(long_messages, 150)
    check("trim_to_budget keeps the newest turn",
          trimmed[-1]["content"] == "z" * 10, trimmed)
    check("trim_to_budget drops the oldest first",
          len(trimmed) == 2 and trimmed[0]["content"] == "y" * 100,
          trimmed)
    check("trim_to_budget keeps a single oversized turn",
          len(trim_to_budget([{"role": "user", "content": "q" * 500}],
                             10)) == 1)
    kept_system = trim_to_budget(
        [{"role": "system", "content": "sys"}] + long_messages, 120)
    check("trim_to_budget never drops the system prompt",
          kept_system[0]["content"] == "sys", kept_system)

    models = [{"id": "meta-llama/x:free", "name": "Llama Free"},
              {"id": "openrouter/free", "name": "Auto"},
              {"id": "paid/model", "name": "Paid",
               "pricing": {"prompt": "0.000002"}}]
    free = free_model_choices(models, True)
    check("free_model_choices filters paid models",
          [pair[0] for pair in free] ==
          ["meta-llama/x:free", "openrouter/free"], free)
    check("free_model_choices can keep everything",
          len(free_model_choices(models, False)) == 3)

    chunks, dropped = render_transcript(
        [{"role": "user", "content": "hello"},
         {"role": "assistant", "content": "hi there"}])
    check("render_transcript labels the speakers",
          chunks[0][1].startswith("You:") and
          chunks[1][1].startswith("Assistant:"), chunks)
    check("render_transcript drops nothing when it fits", dropped == 0)

    many = [{"role": "user", "content": "m" * 100} for _ in range(50)]
    _chunks, dropped = render_transcript(many, budget=500)
    check("render_transcript trims to the budget", dropped > 0, dropped)
    _chunks, dropped = render_transcript(
        [{"role": "user", "content": "only"}], budget=1)
    check("render_transcript always shows the newest turn", dropped == 0)

    state = composer_state("hello", False)
    check("composer_state enables Send for text",
          state["send_enabled"] and state["send_label"] == "Send", state)
    check("composer_state disables Send when empty",
          not composer_state("   ", False)["send_enabled"])
    check("composer_state turns into Stop while sending",
          composer_state("hello", True)["send_label"] == "Stop")
    no_key = composer_state("hi", False, "No API key yet -- open Settings.")
    check("composer_state explains a missing key",
          "Settings" in no_key["hint"] and not no_key["send_enabled"],
          no_key)

    spans = highlight_spans("see https://example.com/x ok")
    check("highlight_spans finds a URL",
          spans == [(4, 25, "link")], spans)
    found = highlight_spans("alpha beta alpha", ["alpha"])
    check("highlight_spans finds every keyword",
          [span for span in found if span[2] == "match"] ==
          [(0, 5, "match"), (11, 16, "match")], found)
    check("highlight_spans skips keywords inside URLs",
          [s for s in highlight_spans("http://a.com/alpha", ["alpha"])
           if s[2] == "match"] == [])

    report = context_report([{"content": "a" * 400}], 200)
    check("context_report computes a percentage",
          report["used"] == 104 and report["percent"] == 52, report)
    check("context_report never divides by zero",
          context_report([{"content": "x"}], 0)["budget"] == 1)
    check("_compact formats thousands", _compact(1500) == "1.5k",
          _compact(1500))

    chats = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    check("next_chat_index skips the deleted chat",
          next_chat_index(chats, "a") == 1, next_chat_index(chats, "a"))
    check("next_chat_index copes with an empty list",
          next_chat_index([], "a") == 0)

    chat = {"title": "Test", "model": "openrouter/free",
            "messages": [{"role": "user", "content": "q"},
                         {"role": "assistant", "content": "a"},
                         {"role": "system", "content": "hidden"}]}
    markdown = export_markdown(chat)
    check("export_markdown has both speakers",
          "## You" in markdown and "## Assistant" in markdown, markdown)
    check("export_markdown hides system turns", "hidden" not in markdown)

    check("usage_line summarises tokens",
          "1.5k tokens" in usage_line({"total_tokens": 1500,
                                       "prompt_tokens": 1000,
                                       "completion_tokens": 500}),
          usage_line({"total_tokens": 1500, "prompt_tokens": 1000,
                      "completion_tokens": 500}))
    check("usage_line tolerates no usage", usage_line(None) == "")

    # the shared core must still work through this module
    check("core self-test agrees",
          core._self_test() == 0, "the shared core self-test failed")

    _print_line("")
    if failures:
        _print_line("%d check(s) failed" % len(failures))
        return 1
    _print_line("All checks passed.")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv or "-V" in argv:
        _print_line("%s %s" % (APP_TITLE, APP_VERSION))
        return 0
    if "--self-test" in argv:
        return _self_test()

    for index, arg in enumerate(argv):
        if arg == "--data-dir" and index + 1 < len(argv):
            os.environ["AQUASNOWLLM_HOME"] = argv[index + 1]

    try:
        tk, ttk, messagebox, filedialog = _import_tk()
    except ImportError:
        sys.stderr.write(
            "%s needs tkinter, which is part of Python but is sometimes\n"
            "split into a separate package:\n\n"
            "  Debian/Ubuntu:  sudo apt-get install python3-tk\n"
            "  MacPorts:       sudo port install py27-tkinter\n"
            "  Mac (python.org installer): already included\n" % APP_TITLE)
        return 2

    config = Config()
    store = ChatStore()
    root = tk.Tk()
    try:
        # ttk themes: 'aqua' on OS X, 'clam'/'default' elsewhere
        style = ttk.Style(root)
        for candidate in ("aqua", "clam", "default"):
            if candidate in style.theme_names():
                style.theme_use(candidate)
                break
    except Exception:
        pass

    ChatApp(root, config, store, tk, ttk, messagebox, filedialog)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# AquaSnowLLM

A lightweight, native-feeling **Aqua chat client for [OpenRouter](https://openrouter.ai)**,
written for **Python 2.7 + PyQt4** so it runs on Macs as old as **Mac OS X 10.6
Snow Leopard** — including the free API keys and `:free` models, and including
machines whose OpenSSL is far too old to complete a modern TLS handshake.

```
┌────────────────────────────────────────────────────────────────────────┐
│ AquaSnowLLM                       [Model: openrouter/free ▾] ☑ Free Only│
│ ┌──────────────┬─────────────────────────────────────────────────────┐ │
│ │ Chats        │ Assistant (openrouter/free)            14:02        │ │
│ │  Hi there!   │ Hello! Ask me anything...                            │ │
│ │  14:02 - 3 ms│ ─────────────────────────────────────────────────── │ │
│ │  Weather     │ You                                     14:01        │ │
│ │  Yesterday   │ What's a good free model for code?                   │ │
│ │              │                                                     │ │
│ │ [+ New Chat] │ ┌─────────────────────────────────────────────────┐ │ │
│ │ [ Delete  ]  │ │ Ask anything...                                 │ │ │
│ └──────────────┴ │                                                 │ │ │
│                  └─────────────────────────────────────────────────┘ │ │
│ Ready.                        Return sends. Shift+Return adds a line  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Contents

1. [What it does](#what-it-does)
2. [Install on Snow Leopard (or newer)](#install-on-snow-leopard-or-newer)
3. [Configure OpenRouter](#configure-openrouter)
4. [Free keys, `:free` models and `openrouter/free`](#free-keys-free-models-and-openrouterfree)
5. [The TLS problem on old Macs](#the-tls-problem-on-old-macs)
6. [Get the `AquaSnowLLM.app`](#get-the-aquasnowllmapp)
7. [Files and data](#files-and-data)
8. [Command-line options](#command-line-options)
9. [Testing](#testing)
10. [Troubleshooting](#troubleshooting)
11. [Design notes](#design-notes)

---

## What it does

* **Chats** live in a sidebar and are saved to `~/.aquasnowllm/chats.json`
  (UTF-8, written atomically, owner-only permissions).
* **Models**: an editable dropdown pre-filled with popular `:free` models,
  defaulting to `openrouter/free` so a brand-new free key works immediately.
  *Refresh Models* pulls the live list from `GET /api/v1/models`; *Free Only*
  filters it down to the models that cost nothing.
* **Threads**: every network call runs on a `QThread`, so the Aqua interface
  never freezes — the Send button becomes *Stop* while a reply is in flight.
* **Errors** land in the transcript as readable, selectable alerts with the
  server's own message underneath: 401 (bad key), 402 (paid model), 403, 404,
  429 (rate limit, with the retry delay), 5xx, timeouts and TLS failures.
* **Settings** (`⌘,`) cover the API key, a custom base URL/proxy, a custom
  `cacert.pem`, temperature, max tokens, timeout, extra headers and a system
  prompt — all stored in `~/.aquasnowllm/config.json`.
* **Offline-first**: standard library only (`urllib2`, `ssl`, `json`).
  `requests` is *not* required.

## Install on Snow Leopard (or newer)

> ### You must install PyQt4. Python 2.7 is not enough.
>
> Mac OS X 10.6 ships **Python 2.6.1**, not 2.7 -- so even the interpreter
> has to be installed. (2.7 arrives with Lion; 2.6 is too old for this app.)
> And Python never includes **Qt**, which AquaSnowLLM needs to open its
> window. Without
> one the app exits with *"AquaSnowLLM needs a Qt 4 binding (PyQt4 or
> PySide)"*. Installing py2app does **not** install PyQt4 — they are
> separate packages, and you need both.

```sh
# 1. Xcode 3.2.6 / 4.x command line tools are required by MacPorts
#    (or the "Command Line Tools" package from Apple's developer site)

# 2. MacPorts itself (choose the port for the machine's OS / arch)
#    https://www.macports.org/install.php

# 3. the interpreter, the Qt 4 binding, and (for the .app) the packager
sudo port install python27
sudo port install py27-pyqt4       # <- required, this is Qt itself
sudo port select --set python python27
sudo port select --set python2 python27
```

Check it worked before going further:

```sh
/opt/local/bin/python2.7 -c "import PyQt4.QtGui; print('Qt4 binding OK')"
```

Then run it:

```sh
git clone https://github.com/aquasnowllm.git
cd aquasnowllm
/opt/local/bin/python2.7 aquasnowllm.py
```

No `pip install` is needed — the app uses only the standard library.
If you *want* `requests` (it is not used by default):

```sh
sudo port install py27-requests
# or:  /opt/local/bin/python2.7 -m pip install requests
```

**Other OS versions**

| System | Notes |
| --- | --- |
| 10.6 Snow Leopard | MacPorts recipe above; `arch = i386` or `x86_64` depending on the Mac |
| 10.7 – 10.9 | Same MacPorts recipe works; use the current MacPorts for that OS |
| 10.10 – 10.14 | `brew install python2 pyqt@4` also works; build the `.app` here |
| 10.15+ | Python 2 and Qt 4 are gone from Homebrew — build on an older OS, or run `tools/gui_smoke.py`-style tests only |

> **Note** PyQt4 was never published as a wheel for Python 2. Running from
> source with MacPorts (or the PyQt4 `.dmg` from riverbankcomputing.com) is
> the supported path.

## Configure OpenRouter

1. Create a free key at <https://openrouter.ai/keys> (it starts `sk-or-v1-`).
2. Launch AquaSnowLLM, press **Settings** (or `⌘,`).
3. Paste the key, leave *Custom Base URL* empty, press **Save**.
4. Pick a model — `openrouter/free` is selected by default — type a message
   and press **Return**.

Requests are sent exactly as OpenRouter documents:

```
POST https://openrouter.ai/api/v1/chat/completions
Authorization: Bearer sk-or-v1-...
Content-Type: application/json
HTTP-Referer: https://github.com/aquasnowllm
X-Title: AquaSnowLLM
```

## Free keys, `:free` models and `openrouter/free`

OpenRouter's free tier has two rules that the app is built around:

* Models whose id ends in **`:free`** cost `$0`. Anything else bills credits —
  with a `$0` balance that request fails with **HTTP 402**.
* **`openrouter/free`** is OpenRouter's automatic router: it selects a free
  model for each request, so a brand-new key with no credits just works.

The app therefore:

* defaults the model dropdown to `openrouter/free`,
* pre-fills `nvidia/nemotron-3.5-super-vl:free`, `google/gemma-4-31b-it:free`,
  `qwen/qwen3.8-27b:free` and a few other free ids,
* lets you **type any custom model id** (it is sent verbatim),
* warns before sending when the selected model is not free (HTTP 402 is
  otherwise guaranteed), and
* re-fetches the live model list — with pricing, so *Free Only* stays
  accurate — from `GET https://openrouter.ai/api/v1/models`.

Free models are rate limited (roughly 20 requests/minute and a daily cap).
A 429 is shown in the chat with the server's `retry_after_seconds` when it
provides one.

## The TLS problem on old Macs

Snow Leopard ships **OpenSSL 0.9.8**, and its Python 2.6/2.7 builds cannot
complete a handshake with most modern HTTPS servers. That is what produces
the classic errors:

```
[Errno 1] _ssl.c:480: error:1407742E:SSL routines:SSL23_GET_SERVER_HELLO:tlsv1 alert protocol version
[Errno 1] _ssl.c:480: error:14090086:SSL routines:SSL3_GET_SERVER_CERTIFICATE:certificate verify failed
```

AquaSnowLLM gives you **three** ways out, in order of ease:

### 1. Let the app use a fresh CA bundle (works when the handshake does)

Just download a current bundle into the data folder — AquaSnowLLM finds it
automatically (or point *Settings → CA Certificate Bundle* at it):

```sh
mkdir -p ~/.aquasnowllm
curl -o ~/.aquasnowllm/cacert.pem https://curl.se/ca/cacert.pem
```

This fixes *certificate verify failed* (expired 2013-era roots). It does not
fix *protocol version* errors — those mean the TLS stack itself is too old,
which is what option 2 is for.

If you build a *private* CA bundle by hand, plain old `openssl` tooling is
fine. Python 3.13 and newer switched on stricter X.509 checks
(`VERIFY_X509_STRICT`), which reject certificates that omit the
`basicConstraints` / `keyUsage` fields; AquaSnowLLM clears that extra
strictness for its own connections so such a bundle keeps working.
Certificate and chain *verification* stay fully enabled — an untrusted or
mismatched certificate is still refused.

### 2. The bundled HTTP → HTTPS bridge (works even with old OpenSSL)

The bridge listens on `http://127.0.0.1:8787` and forwards to
`https://openrouter.ai`, performing the TLS leg itself:

```
AquaSnowLLM  ──(plain HTTP, localhost)──▶  bridge  ──(TLS 1.2)──▶  openrouter.ai
```

Turn it on in two steps:

1. **Settings → Use Bridge** (fills in `http://127.0.0.1:8787` and enables
   *Start the bridge when AquaSnowLLM launches*), **Save**.
2. Restart the app, or use **Tools → Start Bridge**.

The bridge refuses any host other than `openrouter.ai`, because it carries
your API key and must never become an open proxy. It runs in a background
thread inside the app — no extra process to manage.

You can also run it standalone (useful for debugging, or for other tools):

```sh
python2.7 aquasnowllm.py --bridge-only --port 8787
```

### 3. Use an external tunnel

Any local TLS terminator works too — point *Custom Base URL* at it:

| Tool | Command / setup | Base URL |
| --- | --- | --- |
| **stunnel** | `stunnel` with `accept = 8787`, `connect = openrouter.ai:443` | `http://127.0.0.1:8787` |
| **polipo** | `proxyPort = 8123` (HTTP proxy) | `http://127.0.0.1:8123` * |
| **mitmproxy** | `mitmdump -p 8080` | `http://127.0.0.1:8080` * |
| **socat** | `socat TCP-LISTEN:8787,fork OPENSSL:openrouter.ai:443` | `http://127.0.0.1:8787` |

\* HTTP *proxies* need `CONNECT`; the bundled bridge and stunnel/socat are
plain forwarders and are the ones the app is tested against. Any URL that
ends in `/api/v1` is accepted, and a bare host gets `/api/v1` appended.

> **Security:** a plain `http://` base URL outside `localhost` is flagged in
> the status bar, because it would send your API key unencrypted.

## Get the `AquaSnowLLM.app`

There are two ways to end up with a double-clickable app. Both produce the
same bundle layout; they differ in whether Python and Qt get copied inside.

### A. Build the bundle now (works on any machine)

```sh
python3 tools/build_app.py --zip      # or: make dmg
```

That writes `dist/AquaSnowLLM.app` and `dist/AquaSnowLLM.app.zip`. The
bundle is a normal Mac application — `Contents/Info.plist`, a launcher in
`Contents/MacOS/`, the Python source in `Contents/Resources/` — so you can
copy the zip to a Mac, double-click it, and the app runs. It is **not**
frozen: it uses a Python 2.7 and PyQt4 (or PySide) already on the Mac.
On 10.6 both have to be installed -- the system Python there is 2.6.1:

```sh
sudo port install python27 py27-pyqt4
```

The launcher looks for an interpreter that can `import PyQt4.QtGui`
(MacPorts first, then `/usr/bin/python2.7`) and shows a native alert
pointing at the README if it finds none.

This route exists because freezing is the part that truly requires macOS:
py2app copies the local Python framework and Qt dylibs, so it cannot run
from Linux or Windows. `tools/app_test.py` checks the bundle structure,
the plist keys LaunchServices needs, the launcher's exec bit, that no
absolute build paths leak in, and that the zip round-trips — `make
apptest` runs it.

### B. Build a frozen, self-contained bundle on the Mac (py2app)

**Two packages are required, not one.** py2app is only the packager; Qt 4
has to be installed separately or the frozen app will build "successfully"
and then fail to start with a Qt import error:

```sh
sudo port install py27-pyqt4     # Qt itself  <- forget this and it breaks
sudo port install py27-py2app    # the packager
```

Verify both before building — this is the step that catches a missing Qt:

```sh
/opt/local/bin/python2.7 -c "import PyQt4.QtGui; print('Qt4 OK')"
/opt/local/bin/python2.7 -c "import py2app; print('py2app OK')"
```

Then build:

```sh
cd aquasnowllm
python2.7 setup.py py2app
open dist/AquaSnowLLM.app
```

Result: `dist/AquaSnowLLM.app`, a standalone bundle for **Mac OS X 10.6+**
with Python and Qt copied inside (expect ~150–250 MB).

If you install py2app with **pip** rather than MacPorts, pin **0.28.9**:

```sh
python2.7 -m pip install "py2app==0.28.9"
```

Do not use 0.28.10. Despite advertising a Python 2.7 classifier, its
metadata declares `requires_python >= 3.10`, so pip refuses it on 2.7.
At 0.28.9 and earlier the `requires_python` field is absent, which is what
lets it install there.

Useful variations:

```sh
# alias mode: instant rebuilds while developing, needs the interpreter
python2.7 setup.py py2app -A

# shrink the bundle (strips docstrings and PDBs)
python2.7 setup.py py2app --optimize 2

# zip it for another Mac
cd dist && zip -ry AquaSnowLLM-1.0.0.zip AquaSnowLLM.app
```

Caveats worth knowing:

* **PyQt4 is not optional and py2app does not bring it.** A machine with
  Python 2.7 + py2app but no PyQt4 will produce a `.app` that crashes on
  launch. If `import PyQt4.QtGui` fails, stop and install `py27-pyqt4`.
* py2app must run **on macOS**; it cannot cross-build from Linux. Use
  route A above if you only have a non-Mac machine.
* A frozen bundle is self-contained (no PyQt4 install needed on the target
  Mac) but is tied to the OS and architecture it was built on.
* The bundle's `CFBundleIdentifier` is `com.aquasnowllm.app`, minimum system
  version is `10.6`, and the architecture defaults to `x86_64` — edit `arch`
  in `setup.py` for `i386` or a Universal binary.
* Drop an `assets/AquaSnowLLM.icns` in place and it is used as the app icon
  (and an `assets/cacert.pem` is bundled into `Contents/Resources`).
* If you build with MacPorts Python, the bundle embeds that interpreter and
  is therefore tied to the OS it was built on.
* Frozen bundles are large (~150–250 MB) because they carry Python and Qt.
  Route A is ~36 KB but needs PyQt4 on the target Mac; pick per audience. Build with Apple's
  `/usr/bin/python2.7` to depend only on system frameworks — but then PyQt4
  must be installed for *that* interpreter (the `.dmg` from riverbank).

## Files and data

Everything lives in one folder, `~/.aquasnowllm/` (override with
`AQUASNOWLLM_HOME`):

| Path | Contents |
| --- | --- |
| `config.json` | API key (mode `0600`), base URL, cacert path, model, generation settings, bridge address |
| `chats.json` | Up to 200 conversations, UTF-8, atomically replaced on every save |
| `cacert.pem` | Optional CA bundle; found automatically when present (see above) |

Both JSON files are written atomically (temp file + `os.rename`), so a crash
or a full disk cannot truncate your history. A damaged `chats.json` is moved
to `*.corrupt-YYYYMMDD-HHMMSS` and a fresh one is started; a damaged
`config.json` is copied aside and any readable `"field": "value"` pairs are
salvaged, so the API key is never lost. Either way the app
explains this in the status bar instead of dying.

**Repo layout**

```
aquasnowllm.py              the whole application (Python 2.7, PyQt4)
setup.py                    py2app config: frozen .app, built on macOS
tools/build_app.py          .app bundle + zip, buildable on any OS
tools/app_test.py           checks the built bundle and its zip
tools/gui_smoke.py          headless GUI test (PyQt5 alias, modern machine)
tools/py2_syntax_check.py   "no Python 3 syntax" gate for CI
tests/net_test.py           offline TLS/bridge/API integration test
requirements.txt            explains that there are no pip requirements
Makefile                    make check / run / bridge / app / clean
assets/README.txt           bundled resource placeholder
.github/workflows/checks.yml  CI: syntax gate, logic, TLS, window, bundle
```

## Command-line options

```
python2.7 aquasnowllm.py                 start the application
python2.7 aquasnowllm.py --version       print the version
python2.7 aquasnowllm.py --data-dir DIR  use DIR instead of ~/.aquasnowllm
python2.7 aquasnowllm.py --bridge-only   run the HTTP-to-HTTPS bridge only
python2.7 aquasnowllm.py --self-test     run the offline self test
```

`--bridge-only` accepts `--port`/`-p` and `--host`.

## Testing

Three layers, all offline (no key needed, nothing leaves the machine):

```sh
# 1. pure Python logic, both interpreters
python3 aquasnowllm.py --self-test
python2.7 aquasnowllm.py --self-test

# 2. real TLS + custom CA + the bridge, against a local fake OpenRouter
python2.7 tests/net_test.py        # (needs the `openssl` CLI)

# 3. the actual QMainWindow, headless (modern machine; needs PyQt5)
python3 tools/gui_smoke.py

# 4. the Python 2.7 syntax gate
python3 tools/py2_syntax_check.py

# 5. build the .app and verify the bundle
python3 tools/build_app.py --zip
python3 tools/app_test.py
```

`tools/py2_syntax_check.py` compiles the sources with CPython 2.7 when it is
available and otherwise runs `lib2to3` as a parser gate, so a stray f-string
or annotation cannot sneak in.

## Troubleshooting

**`ImportError: No module named PyQt4`**
Install `py27-pyqt4` with MacPorts (see above) and make sure you run
`/opt/local/bin/python2.7`, not `/usr/bin/python2.7`.

**`ImportError: No module named _ssl`** (MacPorts Python on 10.6)
Reinstall the port against a working OpenSSL:
`sudo port install openssl && sudo port install python27 +ssl`.

**`error:1407742E ... tlsv1 alert protocol version`**
Your OpenSSL cannot speak TLS 1.2. Use the bundled bridge (option 2 above).

**`certificate verify failed`**
The system root store is from 2013. Set *CA Certificate Bundle* to a fresh
`cacert.pem` (option 1 above).

**HTTP 401** — the key is wrong, expired, or revoked: *Settings* and paste a
new one from <https://openrouter.ai/keys>.

**HTTP 402** — the model is not free. Choose `openrouter/free` or any id
ending in `:free`.

**HTTP 429** — free-tier rate limit. Wait (the alert shows the server's
suggested delay) or spread requests out.

**The app shows a blank window / icons look wrong**
Some Qt/macOS combinations need the native style forced. The app already
calls `QApplication.setStyle("Macintosh")` when the platform provides it;
the current style is written to the status bar at launch.

**Nothing saves, or "check the folder permissions"**
`~/.aquasnowllm` must be writable by your account. `chmod 700 ~/.aquasnowllm`
or remove the folder and let the app recreate it.

## Design notes

* **Python 2.7 only, deliberately.** The source contains no f-strings, no
  annotations, no `nonlocal`, no `urllib.request`, no `pathlib` — and
  `from __future__ import unicode_literals` at the top, with explicit
  `text_type`/`binary_type` helpers at every UTF-8 boundary. A JSON payload
  containing emoji survives Python 2's `json` module (which otherwise
  produces lone surrogates) because payloads are encoded with `utf8_json()`
  and decoded with `decode_json()`.
* **Three transports, best first.** A local bridge if one is configured,
  then `ssl` + `socket` (honouring a custom CA bundle), then `urllib2` with
  an `HTTPSHandler` wired to the same bundle. The failure detail from each
  attempt is collected, so an error message can say *why* it gave up.
* **Qt 4 signal syntax.** New-style `connect()`/`pyqtSignal` (PyQt4 ≥ 4.4)
  is used throughout instead of `QObject.connect`, which old PyQt4 accepted
  but PyQt ≥ 4.5 deprecated.
* **Threading.** `ApiWorker(QThread)` emits a result object; the GUI thread
  renders it. *Stop* sets a cancel event and detaches the worker's signals,
  so a late reply can never touch the UI. The bridge is a plain Python
  thread wrapped by a `QThread` purely so Qt can own its lifetime.

---

MIT licensed. Built for the machines that still work.

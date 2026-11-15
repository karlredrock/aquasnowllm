#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AquaSnowLLM -- a small, native-feeling Aqua chat client for OpenRouter.

Written for Python 2.7, targeting Mac OS X 10.6 Snow Leopard (whose own
system Python is 2.6.1, so 2.7 comes from MacPorts or python.org)
using PyQt4 or PySide (Qt 4).  It is deliberately conservative:

  * no Python 3 syntax anywhere (no f-strings, no annotations, no
    ``nonlocal``, no ``yield from``)
  * no third party HTTP libraries -- ``urllib2``/``ssl`` from the standard
    library, plus a tiny HTTP/1.1 client for the local bridge
  * works even when OpenSSL is too old for a TLS handshake (OS X 10.6
    ships OpenSSL 0.9.8): point the app at the bundled HTTP-to-HTTPS
    bridge, or at stunnel/polipo/mitmproxy, from the Settings dialog

The file also runs unmodified on Python 3 when a Qt 4 binding is present
(useful for development on a modern machine); every version specific bit
sits behind a guard.

File layout
-----------
  1. helpers, text/JSON/SSL utilities
  2. Config        -- ~/.aquasnowllm/config.json
  3. ChatStore     -- ~/.aquasnowllm/chats.json (UTF-8, atomic writes)
  4. LanguageModel -- OpenRouter API client (pure Python, no Qt)
  5. HTTPBridge    -- local HTTP -> HTTPS bridge (pure Python, no Qt)
  6. Qt 4 object graph: worker thread, chat view, settings dialog,
     main window
  7. entry point (GUI, ``--bridge-only``, ``--self-test``)

Usage
-----
    python2.7 aquasnowllm.py
"""

from __future__ import unicode_literals

import codecs
import datetime
import json
import os
import platform
import random
import re
import socket
import ssl
import sys
import tempfile
import threading
import time

try:  # Python 2: urlparse() lives in its own module
    from urlparse import urlparse
    from urllib2 import HTTPError, URLError
except ImportError:  # Python 3 development path
    from urllib.parse import urlparse
    from urllib.error import HTTPError, URLError

APP_NAME = "AquaSnowLLM"
APP_VERSION = "1.0.0"
APP_URL = "https://github.com/aquasnowllm"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CHAT_COMPLETIONS_PATH = "/chat/completions"
MODELS_PATH = "/models"

USER_AGENT = "%s/%s (%s)" % (
    APP_NAME, APP_VERSION, platform.system() or "unknown")

#: Shipped so the picker is useful before the first "Refresh Models".
#: (Refresh it from OpenRouter for the live list.)
DEFAULT_MODELS = [
    "openrouter/free",
    "nvidia/nemotron-3.5-super-vl:free",
    "google/gemma-4-31b-it:free",
    "qwen/qwen3.8-27b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-r1:free",
    "mistralai/mistral-small-3.2-24b-instruct:free",
]
DEFAULT_MODEL = DEFAULT_MODELS[0]

TITLE_MAX = 44
MAX_REDIRECTS = 6

PY2 = sys.version_info[0] == 2

# ``u""`` is unicode on both interpreters, ``b""`` is bytes on both, so the
# types can be derived without touching a name that only exists on one of
# them (which would upset linters and readers alike).
text_type = type(u"")
binary_type = type(b"")


# ---------------------------------------------------------------------------
# 1. Helpers
# ---------------------------------------------------------------------------

_TRANSLITERATIONS = {
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"',
    0x2013: "-", 0x2014: "--", 0x2015: "--", 0x2212: "-",
    0x2026: "...", 0x2022: "*", 0x00B7: "-",
    0x00A0: " ", 0x2007: " ", 0x2009: " ", 0x202F: " ", 0x200A: " ",
    0x200B: "", 0x200C: "", 0x200D: "", 0xFEFF: "",
    0x2039: "<", 0x203A: ">", 0x00AB: '"', 0x00BB: '"', 0x00AD: "-",
}
_TRANSLITERATION_TABLE = dict(
    (codepoint, text_type(replacement))
    for codepoint, replacement in _TRANSLITERATIONS.items())


def to_text(value):
    """Return ``value`` as unicode text, tolerating bytes/None/numbers."""
    if value is None:
        return text_type("")
    if isinstance(value, text_type):
        return value
    if isinstance(value, binary_type):
        for encoding in ("utf-8", "latin-1"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", "replace")
    return text_type(value)


def to_bytes(value, encoding="utf-8"):
    """Return ``value`` as bytes (``str`` on Python 2)."""
    if isinstance(value, binary_type):
        return value
    if not isinstance(value, text_type):
        value = to_text(value)
    return value.encode(encoding, "replace")


def utf8_json(obj):
    """``json.dumps`` that always yields UTF-8 bytes on Python 2.

    ``json.dumps()`` returns ``str`` *unless* the payload contains
    non-ASCII characters, in which case Python 2 returns ``unicode``.
    Encoding it ourselves keeps json's surrogate-pair escaping valid.
    """
    text = json.dumps(obj, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"))
    return to_bytes(text)


def decode_json(payload):
    """Decode JSON bytes, normalising surrogate pairs (py2 ``json`` bug)."""
    if isinstance(payload, binary_type):
        payload = payload.decode("utf-8", "replace")
    payload = payload.encode("utf-8", "replace").decode("utf-8", "replace")
    return json.loads(payload)


def transliterate(text):
    """Fold typographic punctuation to ASCII for old on-screen fonts."""
    if not text:
        return text_type("")
    text = to_text(text)
    # ``unicode.translate`` accepts a dict of codepoint -> replacement and
    # has the same semantics on Python 2 and Python 3.
    return text.translate(_TRANSLITERATION_TABLE)


def shorten(text, limit):
    """Collapse whitespace and clip ``text`` to ``limit`` characters."""
    text = re.sub(r"\s+", " ", to_text(text)).strip()
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 3)].rstrip() + text_type("...")


def ellipsis(text, limit=TITLE_MAX):
    """Shorten to ``limit`` characters, appending an ASCII ellipsis."""
    text = re.sub(r"\s+", " ", to_text(text)).strip()
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 3)].rstrip() + text_type("...")


def format_elapsed(seconds):
    seconds = int(seconds or 0)
    if seconds < 1:
        return text_type("less than a second")
    if seconds == 1:
        return text_type("1 second")
    if seconds < 60:
        return text_type("%d seconds") % seconds
    minutes = seconds // 60
    rest = seconds % 60
    if minutes == 1:
        return text_type("1 minute %d seconds") % rest
    return text_type("%d minutes %d seconds") % (minutes, rest)


def default_data_dir():
    """``~/.aquasnowllm`` (overridable with ``AQUASNOWLLM_HOME``)."""
    override = os.environ.get("AQUASNOWLLM_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".aquasnowllm")


def atomic_write_text(path, text, mode=0o600):
    """Write UTF-8 ``text`` to ``path`` atomically with restrictive mode.

    Returns ``True`` on success, ``False`` when the location is not
    writable -- callers surface that in the GUI rather than crashing,
    which matters when the app is double clicked from Finder.
    """
    data = to_bytes(text)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_path = None
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory, 0o700)
        handle = tempfile.NamedTemporaryFile(
            mode="wb", dir=directory, prefix=".tmp-", delete=False)
        tmp_path = handle.name
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
        if os.path.exists(path):
            try:
                os.chmod(tmp_path, os.stat(path).st_mode & 0o777)
            except OSError:
                pass
        os.rename(tmp_path, path)
        return True
    except (OSError, IOError, UnicodeError):
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def read_text_file(path):
    """Read a UTF-8 text file, returning ``None`` when it is absent."""
    if not path or not os.path.exists(path):
        return None
    handle = codecs.open(path, "r", "utf-8")
    try:
        return handle.read()
    finally:
        handle.close()


def new_id(prefix="chat"):
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
    return "%s-%s-%04d" % (prefix, stamp, random.randint(0, 9999))


def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def friendly_time(iso_string):
    """Render an ISO timestamp as a short local label for the sidebar."""
    if not iso_string:
        return text_type("")
    try:
        cleaned = to_text(iso_string).rstrip("Z")
        stamp = datetime.datetime.strptime(cleaned[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return text_type("")
    try:
        import calendar

        local_seconds = calendar.timegm(stamp.timetuple())
        local = datetime.datetime.fromtimestamp(local_seconds)
    except (ValueError, OverflowError, OSError):
        local = stamp
    today = datetime.date.today()
    if local.date() == today:
        return local.strftime("%H:%M")
    if (today - local.date()).days == 1:
        return text_type("Yesterday")
    return local.strftime("%b %d")


def variant_to_text(value):
    """Unwrap a ``QVariant``/``QString`` (or plain Python object) to text."""
    if value is None:
        return text_type("")
    to_string = getattr(value, "toString", None)
    if to_string is not None:
        try:
            return to_text(to_string())
        except Exception:
            pass
    return to_text(value)


# --- TLS / proxy helpers ---------------------------------------------------

def expand_path(path):
    """Expand ``~`` and environment variables in a user supplied path."""
    path = to_text(path).strip()
    if not path:
        return text_type("")
    if path.startswith("~"):
        path = os.path.expanduser(path)
    return os.path.expandvars(path)


def find_bundle_file(name):
    """Locate a data file inside the running .app bundle (py2app layout)."""
    candidates = []
    if hasattr(sys, "frozen"):
        try:
            import Foundation  # noqa: F401  (shipped by py2app bundles)

            resource = Foundation.NSBundle.mainBundle().resourcePath()
            if resource:
                resource = to_text(resource)
                candidates.append(os.path.join(resource, "Resources", name))
                candidates.append(os.path.join(resource, name))
        except Exception:
            pass
        if getattr(sys, "prefix", None):
            candidates.append(os.path.join(sys.prefix, "Resources", name))
        if getattr(sys, "executable", None):
            exe_dir = os.path.dirname(sys.executable)
            candidates.append(
                os.path.join(os.path.dirname(exe_dir), "Resources", name))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "assets", name))
    candidates.append(os.path.join(here, name))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return text_type("")


def resolve_cacert_path(config_value):
    """Turn the user's cacert setting into a file path ('' = system).

    Order: the configured path, then ``~/.aquasnowllm/cacert.pem``, then a
    bundle shipped inside the ``.app`` -- so a user who downloads a fresh
    bundle once gets TLS working without touching the settings.
    """
    value = expand_path(config_value)
    if not value:
        for candidate in (os.path.join(default_data_dir(), "cacert.pem"),
                          find_bundle_file("cacert.pem")):
            if candidate and os.path.isfile(candidate):
                return candidate
        return text_type("")
    if os.path.isdir(value):
        for candidate in ("cacert.pem", "cacert.crt", "ca-bundle.crt"):
            joined = os.path.join(value, candidate)
            if os.path.isfile(joined):
                return joined
    return value


def make_ssl_context(cacert_path=""):
    """Build an ``ssl.SSLContext``, honouring a custom CA bundle.

    Returns ``(context, warnings)``.  Old interpreters (2.7.0 .. 2.7.8)
    lack ``create_default_context``; the fallback uses ``PROTOCOL_SSLv23``
    with SSLv2/SSLv3 disabled, which is what modern servers expect.
    """
    warnings = []
    path = to_text(cacert_path or "")
    if path and not os.path.isfile(path):
        warnings.append("CA bundle %s was not found; using the system "
                        "certificates." % path)
        path = text_type("")
    if hasattr(ssl, "create_default_context"):
        try:
            return ssl.create_default_context(cafile=path or None), warnings
        except Exception as exc:  # pragma: no cover - corrupted bundle
            warnings.append("Could not load %s (%s); using the system "
                            "certificates." % (path, exc))
            return ssl.create_default_context(), warnings
    context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)  # pragma: no cover
    for option in ("OP_NO_SSLv2", "OP_NO_SSLv3"):
        if hasattr(ssl, option):
            context.options |= getattr(ssl, option)
    try:
        if path:
            context.load_verify_locations(path)
        else:
            context.load_default_certs()
    except Exception as exc:
        warnings.append("Could not load certificates (%s)." % exc)
    return context, warnings


def wrap_tls(context, sock, host):
    """TLS-wrap ``sock`` and verify the certificate against ``host``.

    ``ssl`` differs wildly across the 2.7 maintenance releases:

    * 2.7.0 .. 2.7.8   -- no ``SSLContext.wrap_socket`` keyword arguments
      at all, and no ``ssl.match_hostname``.
    * 2.7.9 .. 2.7.17  -- ``server_hostname=`` enables SNI, but the host
      name check (CPython bpo-23749) validates the *connected address*
      instead, so a certificate legitimately issued for ``localhost`` is
      rejected when ``127.0.0.1`` was dialled.

    This wrapper uses SNI wherever available and falls back to an explicit
    ``ssl.match_hostname`` check against the intended host, so certificates
    are still verified -- just against the right name.
    """
    host = to_text(host)
    try:
        wrapped = context.wrap_socket(sock, server_hostname=host)
    except TypeError:  # Python 2.7.0 .. 2.7.8: no SNI keyword
        wrapped = context.wrap_socket(sock)

    matcher = getattr(ssl, "match_hostname", None)
    if matcher is not None and not getattr(context, "check_hostname", False):
        matcher(wrapped.getpeercert(), host)
    return wrapped


def connect_tls(host, port, cacert_path, timeout):
    """Open a TLS connection to ``host:port``, verified against a CA bundle.

    Falls back to a manual host name check on the Python 2.7 releases whose
    own check compares the certificate against the resolved IP address
    (see :func:`wrap_tls`); certificate *and* chain verification stay on.
    """
    host = to_text(host)
    context, _warnings = make_ssl_context(cacert_path)
    raw_sock = socket.create_connection((host, port), timeout)
    raw_sock.settimeout(timeout)
    try:
        return wrap_tls(context, raw_sock, host)
    except (socket.error, ValueError) as exc:
        # ``ssl.CertificateError`` derives from ValueError on Python 2.7
        try:
            raw_sock.close()
        except Exception:
            pass
        text = to_text(exc)
        rejected = "doesn't match" in text or "does not match" in text
        if not rejected or not getattr(ssl, "match_hostname", None):
            raise
    relaxed = ssl.SSLContext(getattr(ssl, "PROTOCOL_TLS_CLIENT",
                                     ssl.PROTOCOL_SSLv23))
    relaxed.check_hostname = False
    relaxed.verify_mode = ssl.CERT_REQUIRED
    if cacert_path and os.path.isfile(cacert_path):
        relaxed.load_verify_locations(cacert_path)
    else:
        relaxed.load_default_certs()
    raw_sock = socket.create_connection((host, port), timeout)
    raw_sock.settimeout(timeout)
    wrapped = relaxed.wrap_socket(raw_sock)
    ssl.match_hostname(wrapped.getpeercert(), host)
    return wrapped


def normalize_base_url(value, warn=None):
    """Validate/normalise a base URL, returning ``(url, warnings)``."""
    warnings = []
    url = to_text(value).strip()
    if not url:
        return OPENROUTER_BASE_URL, warnings
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        warnings.append("Unsupported URL scheme %r -- using %s."
                        % (scheme, OPENROUTER_BASE_URL))
        return OPENROUTER_BASE_URL, warnings
    netloc = parsed.netloc or ""
    if not netloc:
        warnings.append("Could not parse %r -- using %s."
                        % (url, OPENROUTER_BASE_URL))
        return OPENROUTER_BASE_URL, warnings
    host = to_text(parsed.hostname or "")
    if scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        warnings.append(
            "Plain HTTP sends the API key unencrypted; use it only for a "
            "local proxy that adds TLS itself.")
    path = parsed.path or ""
    if path.endswith("/"):
        path = path[:-1]
    if path.endswith("/api/v1"):
        pass
    elif path.endswith("/api"):
        path += "/v1"
    elif not path:
        path = "/api/v1"
    elif not path.endswith("/v1"):
        path = path + "/api/v1"
    return "%s://%s%s" % (scheme, netloc, path), warnings


def split_api_base(base_url):
    """Split ``https://host/api/v1`` into ``('https://host', '/api/v1')``."""
    parsed = urlparse(base_url)
    scheme = (parsed.scheme or "https").lower()
    if parsed.netloc:
        host = parsed.netloc
        prefix = (parsed.path or "").rstrip("/")
    else:  # bare host passed without a scheme
        host = parsed.path or ""
        prefix = ""
    if not prefix:
        prefix = "/api/v1"
    return "%s://%s" % (scheme, host), prefix


def is_free_model(model_id, pricing=None):
    """Heuristic used by the client, the filter and the toolbar."""
    model_id = to_text(model_id)
    if model_id.endswith(":free") or model_id == "openrouter/free":
        return True
    if isinstance(pricing, dict):
        try:
            prompt = float(pricing.get("prompt") or 0)
            completion = float(pricing.get("completion") or 0)
        except (TypeError, ValueError):
            return False
        return prompt == 0 and completion == 0
    return False


# --- tiny HTTP/1.1 client for the local bridge ----------------------------

def _dechunk(data):
    """Decode a ``Transfer-Encoding: chunked`` body."""
    parts = []
    while True:
        line_end = data.find(b"\r\n")
        if line_end < 0:
            break
        try:
            size = int(data[:line_end].split(b";")[0], 16)
        except ValueError:
            return b"".join(parts) if parts else data
        if size == 0:
            break
        start = line_end + 2
        parts.append(data[start:start + size])
        data = data[start + size + 2:]
    return b"".join(parts)


class HTTPResponse(object):
    """Minimal response object returned by :class:`SimpleHTTPClient`."""

    def __init__(self, status, reason, headers, body, url):
        self.status = status
        self.reason = reason
        self.headers = headers
        self.content = body
        self.url = url

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)


class SimpleHTTPClient(object):
    """A deliberately naive HTTP/1.1 client (one request per connection).

    ``urllib2`` on Snow Leopard insists on negotiating TLS with the
    system's ancient OpenSSL, which fails against modern servers.  When
    traffic is routed through a local bridge we only speak cleartext HTTP,
    so this small client removes all of urllib2's TLS machinery from the
    hot path.
    """

    def __init__(self, timeout=120):
        self.timeout = timeout

    def _connection(self, scheme, host, port):
        raw_sock = socket.create_connection((host, port), self.timeout)
        raw_sock.settimeout(self.timeout)
        if scheme == "http":
            return raw_sock
        return connect_tls(host, port, "", self.timeout)

    def request(self, method, url, body=None, headers=None):
        headers = dict(headers or {})
        current = url
        for _attempt in range(MAX_REDIRECTS):
            response = self._once(method, current, body, headers)
            if response.status in (301, 302, 303, 307, 308):
                location = response.header("location")
                if location:
                    current = self._absolute(current, location)
                    if response.status in (301, 302, 303):
                        method = "GET"
                        body = None
                        for key in list(headers):
                            if key.lower() == "content-type":
                                del headers[key]
                    continue
            return response
        raise IOError("Too many redirects while contacting the bridge.")

    def _once(self, method, url, body, headers):
        parsed = urlparse(url)
        scheme = (parsed.scheme or "http").lower()
        if scheme not in ("http", "https"):
            raise ValueError("Only http and https URLs are supported.")
        host = to_text(parsed.hostname or "")
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = path + "?" + parsed.query

        lines = [
            "%s %s HTTP/1.1" % (method.upper(), path),
            "Host: %s" % (parsed.netloc or host),
            "Connection: close",
            "Accept-Encoding: identity",
            "User-Agent: %s" % USER_AGENT,
        ]
        names = set()
        for key, value in headers.items():
            lines.append("%s: %s" % (key, value))
            names.add(key.lower())
        if body is not None:
            if "content-length" not in names:
                lines.append("Content-Length: %d" % len(body))
        elif method.upper() == "POST":
            lines.append("Content-Length: 0")
        packet = to_bytes("\r\n".join(lines) + "\r\n\r\n")
        if body:
            packet += body

        sock = self._connection(scheme, host, port)
        try:
            sock.sendall(packet)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        return self._parse(b"".join(chunks), url)

    @staticmethod
    def _absolute(base, location):
        if "://" in location:
            return location
        root = urlparse(base)
        if location.startswith("/"):
            return "%s://%s%s" % (root.scheme, root.netloc, location)
        path = root.path or "/"
        if "/" in path:
            path = path[:path.rfind("/") + 1]
        return "%s://%s%s%s" % (root.scheme, root.netloc, path, location)

    @staticmethod
    def _parse(raw, url):
        split_at = raw.find(b"\r\n\r\n")
        if split_at < 0:
            raise IOError(
                "The bridge returned an empty or malformed HTTP response. "
                "Is the local proxy running?")
        head = raw[:split_at].decode("latin-1")
        data = raw[split_at + 4:]
        lines = head.split("\r\n")
        status_line = lines[0] if lines else ""
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[0].upper().startswith("HTTP/"):
            raise IOError("The bridge did not speak HTTP (%r)." % status_line)
        try:
            status = int(parts[1])
        except ValueError:
            raise IOError("Malformed HTTP status line %r." % status_line)
        reason = parts[2] if len(parts) > 2 else ""
        headers = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key == "set-cookie":
                continue
            headers[key] = value.strip()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            data = _dechunk(data)
        elif "content-length" in headers:
            try:
                length = int(headers["content-length"])
            except ValueError:
                length = len(data)
            data = data[:length]
        return HTTPResponse(status, reason, headers, data, url)


# ---------------------------------------------------------------------------
# 2. Configuration
# ---------------------------------------------------------------------------

CONFIG_DEFAULTS = {
    "api_key": "",
    "base_url": "",
    "cacert": "",
    "model": DEFAULT_MODEL,
    "free_only": True,
    "temperature": 0.7,
    "max_tokens": 1024,
    "timeout": 120,
    "system_prompt": "",
    "bridge_autostart": True,
    "bridge_host": "127.0.0.1",
    "bridge_port": 8787,
    "extra_headers": "",
}

SECRET_FIELDS = ("api_key",)


def codecs_encode_pattern():
    """Regex for ``"key": "value"`` pairs in a (possibly broken) JSON file."""
    return re.compile(r'"([A-Za-z0-9_]+)"\s*:\s*"((?:[^"\\]|\\.)*)"')


class Config(object):
    """Persistent settings stored next to the chats in ``config.json``."""

    def __init__(self, data_dir=None):
        self.data_dir = data_dir or default_data_dir()
        self.path = os.path.join(self.data_dir, "config.json")
        self.data = dict(CONFIG_DEFAULTS)
        self.error = None
        self.corrupt = False
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self):
        try:
            raw = read_text_file(self.path)
        except (IOError, OSError, UnicodeError) as exc:
            self.error = text_type(exc)
            return
        if not raw:
            return
        try:
            stored = json.loads(raw.lstrip("\ufeff"))
        except ValueError as exc:
            # A damaged config must never lose the API key.  Keep the file
            # in place (only copying it aside for inspection) and salvage
            # whatever ``"field": "value"`` pairs are still readable, so a
            # single mangled character does not force a re-entry of the key.
            backup = os.path.join(
                self.data_dir,
                "config.json.corrupt-%s" % time.strftime("%Y%m%d-%H%M%S"))
            try:
                codecs.open(backup, "w", "utf-8").write(raw)
            except (IOError, OSError):
                backup = None
            salvaged = self._salvage(raw)
            for key, value in salvaged.items():
                self.data[key] = value
            self.error = text_type(
                "config.json could not be read (%s).%s"
                % (exc, "" if backup else " It was left untouched."))
            self.corrupt = True
            return
        if isinstance(stored, dict):
            for key, value in stored.items():
                self.data[key] = value

    @staticmethod
    def _salvage(raw):
        """Pull ``"name": "value"`` pairs out of a damaged config file."""
        salvaged = {}
        pattern = codecs_encode_pattern()
        for match in pattern.finditer(raw):
            key = match.group(1)
            value = match.group(2)
            if key not in CONFIG_DEFAULTS:
                continue
            try:
                value = json.loads('"%s"' % value)
            except ValueError:
                continue
            salvaged[key] = value
        return salvaged

    def save(self):
        payload = json.dumps(self.data, indent=2, sort_keys=True,
                             ensure_ascii=False)
        if not atomic_write_text(self.path, payload, mode=0o600):
            self.error = text_type(
                "Could not write %s (check the folder permissions)."
                % self.path)
            return False
        self.error = None
        return True

    # -- typed accessors ---------------------------------------------------
    def get(self, key, default=None):
        if key in self.data:
            return self.data[key]
        if key in CONFIG_DEFAULTS:
            return CONFIG_DEFAULTS[key]
        return default

    def __getitem__(self, key):
        return self.get(key)

    def __setitem__(self, key, value):
        self.data[key] = value

    def api_key(self):
        return to_text(self.get("api_key")).strip()

    def model(self):
        return to_text(self.get("model")).strip() or DEFAULT_MODEL

    def timeout(self):
        try:
            return max(10.0, float(self.get("timeout") or 120))
        except (TypeError, ValueError):
            return 120.0

    def temperature(self):
        try:
            return float(self.get("temperature"))
        except (TypeError, ValueError):
            return 0.7

    def max_tokens(self):
        try:
            return int(self.get("max_tokens") or 1024)
        except (TypeError, ValueError):
            return 1024

    def free_only(self):
        return bool(self.get("free_only", True))

    def base_url(self):
        return normalize_base_url(self.get("base_url"))[0]

    def base_url_warnings(self):
        return normalize_base_url(self.get("base_url"))[1]

    def extra_headers(self):
        """Parse ``Name: value`` lines typed in the settings dialog."""
        result = {}
        for line in to_text(self.get("extra_headers")).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            name, value = line.split(":", 1)
            if name.strip():
                result[name.strip()] = value.strip()
        for reserved in ("authorization", "content-type", "content-length",
                         "host", "connection"):
            for name in list(result):
                if name.lower() == reserved:
                    del result[name]
        return result

    def bridge_host(self):
        return to_text(self.get("bridge_host") or "127.0.0.1").strip()

    def bridge_port(self):
        try:
            return int(self.get("bridge_port") or 8787)
        except (TypeError, ValueError):
            return 8787

    def bridge_url(self):
        return "http://%s:%d" % (self.bridge_host(), self.bridge_port())

    def uses_bridge(self):
        """True when requests go to a cleartext (local proxy) endpoint."""
        return self.base_url().lower().startswith("http://")

    def export(self):
        """Everything except secrets, for bug reports."""
        copy = dict(self.data)
        for field in SECRET_FIELDS:
            if copy.get(field):
                copy[field] = "<redacted>"
        return copy


# ---------------------------------------------------------------------------
# 3. Chat storage
# ---------------------------------------------------------------------------

class ChatStore(object):
    """Saved conversations in ``~/.aquasnowllm/chats.json`` (UTF-8)."""

    def __init__(self, data_dir=None, limit=200):
        self.data_dir = data_dir or default_data_dir()
        self.path = os.path.join(self.data_dir, "chats.json")
        self.limit = limit
        self.chats = []
        self.error = None
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self):
        raw = None
        try:
            raw = read_text_file(self.path)
        except (IOError, OSError, UnicodeError) as exc:
            self.error = text_type(exc)
        if raw:
            try:
                stored = json.loads(raw.lstrip("\ufeff"))
            except ValueError as exc:
                self._backup_corrupt(exc)
            else:
                if isinstance(stored, dict):
                    chats = stored.get("chats") or []
                elif isinstance(stored, list):
                    chats = stored
                else:
                    chats = []
                self.chats = [self._normalize(chat) for chat in chats
                              if isinstance(chat, dict)]
                self.chats.sort(
                    key=lambda chat: chat.get("updated") or "", reverse=True)
                if self.chats:
                    return
        self.chats = [self.new_chat(persist=False)]

    def _backup_corrupt(self, exc):
        backup = os.path.join(
            self.data_dir,
            "chats.json.corrupt-%s" % time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.rename(self.path, backup)
            self.error = text_type(
                "chats.json was damaged (%s); it was moved to %s."
                % (exc, os.path.basename(backup)))
        except OSError:
            self.error = text_type("chats.json was damaged (%s)." % exc)

    def save(self):
        if len(self.chats) > self.limit:
            self.chats.sort(
                key=lambda chat: chat.get("updated") or "", reverse=True)
            del self.chats[self.limit:]
        payload = {
            "version": 1,
            "application": APP_NAME,
            "chats": self.chats,
        }
        text = json.dumps(payload, indent=1, sort_keys=False,
                          ensure_ascii=False)
        if not atomic_write_text(self.path, text, mode=0o600):
            self.error = text_type(
                "Could not save %s (check the folder permissions)."
                % self.path)
            return False
        self.error = None
        return True

    # -- model -------------------------------------------------------------
    @staticmethod
    def _normalize(chat):
        messages = []
        for message in chat.get("messages") or []:
            if not isinstance(message, dict):
                continue
            role = to_text(message.get("role") or "user")
            if role not in ("user", "assistant", "system"):
                role = "user"
            messages.append({
                "role": role,
                "content": to_text(message.get("content")),
                "model": to_text(message.get("model")),
                "ts": to_text(message.get("ts")),
            })
        return {
            "id": to_text(chat.get("id")) or new_id(),
            "title": to_text(chat.get("title")) or text_type("New Chat"),
            "created": to_text(chat.get("created")),
            "updated": to_text(chat.get("updated")),
            "model": to_text(chat.get("model")),
            "messages": messages,
        }

    def new_chat(self, title=None, model=None, persist=True):
        stamp = now_iso()
        chat = {
            "id": new_id(),
            "title": to_text(title) or text_type("New Chat"),
            "created": stamp,
            "updated": stamp,
            "model": to_text(model or ""),
            "messages": [],
        }
        self.chats.insert(0, chat)
        if persist:
            self.save()
        return chat

    def find(self, chat_id):
        for chat in self.chats:
            if chat.get("id") == chat_id:
                return chat
        return None

    def remove(self, chat_id):
        chat = self.find(chat_id)
        if chat is None:
            return False
        self.chats.remove(chat)
        if not self.chats:
            self.chats = [self.new_chat(persist=False)]
        self.save()
        return True

    @staticmethod
    def append_message(chat, role, content, model=None, ts=None):
        message = {
            "role": role,
            "content": to_text(content),
            "model": to_text(model or chat.get("model") or ""),
            "ts": ts or now_iso(),
        }
        chat.setdefault("messages", []).append(message)
        chat["updated"] = message["ts"]
        if role == "user" and content and chat.get("title") in (
                None, "", "New Chat"):
            chat["title"] = ellipsis(content)
        return message

    def touch(self, chat):
        chat["updated"] = now_iso()
        self.chats.sort(key=lambda item: item.get("updated") or "",
                        reverse=True)


# ---------------------------------------------------------------------------
# 4. OpenRouter client (pure Python, no Qt)
# ---------------------------------------------------------------------------

class APIError(Exception):
    """A request failed in a way we can explain to the user."""

    def __init__(self, status, message, detail=None, raw=None,
                 retry_after=None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        self.detail = detail
        self.raw = raw
        self.retry_after = retry_after

    def friendly(self, model=None):
        status = self.status
        detail = self.detail or ""
        if status == 401:
            head = ("OpenRouter rejected the API key (HTTP 401).\n\n"
                    "Open Settings and paste a key from "
                    "https://openrouter.ai/keys -- keys starting with "
                    "\"sk-or-v1-\" are fine, and free keys are enough for "
                    ":free models.")
        elif status == 402:
            head = ("This model is not free for your account (HTTP 402: "
                    "payment required).\n\n"
                    "Pick a model whose id ends in \":free\", or choose "
                    "\"openrouter/free\", which routes to free models "
                    "automatically.")
        elif status == 429:
            head = ("OpenRouter is rate limiting this key (HTTP 429).\n\n"
                    "The free tier allows roughly 20 requests per minute "
                    "and a daily cap. Wait a little%s and try again."
                    % ((" -- the server asked for %s second(s)."
                        % self.retry_after) if self.retry_after else ""))
        elif status == 403:
            head = ("Access to this model was refused (HTTP 403).\n\n"
                    "If it is marked :free, check that your account allows "
                    "free models and any moderation settings.")
        elif status == 404:
            head = ("This model does not exist any more (HTTP 404):\n%s\n\n"
                    "Press \"Refresh Models\" to reload the live list."
                    % (model or "the selected model"))
        elif status in (502, 503, 504):
            head = ("OpenRouter or the upstream provider is unavailable "
                    "(HTTP %d).\n\nThis is usually temporary: try again in "
                    "a moment or pick a different model." % status)
        elif status == 400:
            head = ("OpenRouter did not accept the request (HTTP 400).\n\n"
                    "The model id, the message list or the token limit is "
                    "probably not valid.")
        elif not status:
            head = ("Could not reach OpenRouter.\n\n"
                    "Check the network, then the Settings dialog: an old "
                    "Mac usually needs a fresh cacert.pem or the bundled "
                    "HTTP-to-HTTPS bridge.")
        else:
            head = "The request failed (HTTP %s).\n\n" % status
        if detail:
            return head + "\n\nDetails: " + detail
        return head

    def __str__(self):
        return to_text(self.message)


class LanguageModel(object):
    """Thin, dependency-free client for the OpenRouter HTTP API."""

    def __init__(self, api_key, base_url=None, cacert_path=None,
                 timeout=120, bridge=None, extra_headers=None):
        self.api_key = to_text(api_key).strip()
        url, warnings = normalize_base_url(base_url)
        self.base_url = url
        self.warnings = warnings
        self.cacert_path = to_text(cacert_path or "")
        try:
            self.timeout = float(timeout or 120)
        except (TypeError, ValueError):
            self.timeout = 120.0
        self.bridge = bridge
        self.extra_headers = dict(extra_headers or {})
        self.transport = text_type("none")

    # -- headers -----------------------------------------------------------
    def headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "HTTP-Referer": APP_URL,
            "X-Title": APP_NAME,
        }
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        headers.update(self.extra_headers)
        return headers

    def url_for(self, path):
        return self.base_url.rstrip("/") + path

    # -- transport selection ----------------------------------------------
    def _request(self, method, url, body=None):
        """Return ``(status, body_bytes)`` using the best transport."""
        errors = []
        if self.bridge is not None:
            try:
                status, payload = self.bridge.request(
                    method, url, body, self.headers(), self.timeout)
                self.transport = "local bridge"
                self._check_status(status, payload)
                return status, payload
            except APIError:
                raise
            except Exception as exc:
                errors.append("local bridge: %s" % exc)
        if to_text(url).lower().startswith("https://"):
            try:
                result = self._request_ssl(method, url, body)
                self.transport = "python ssl"
                return result
            except APIError:
                raise
            except Exception as exc:
                errors.append("python ssl: %s" % exc)
        try:
            result = self._request_urllib(method, url, body)
            self.transport = "urllib2"
            return result
        except APIError:
            raise
        except Exception as exc:
            errors.append("urllib2: %s" % exc)
        raise APIError(0, "Could not reach %s" % self.base_url,
                       detail="; ".join(errors))

    def _check_status(self, status, payload):
        if status and 200 <= status < 300:
            return
        detail = text_type("")
        raw_text = text_type("")
        retry_after = None
        try:
            raw_text = payload.decode("utf-8", "replace")
            data = decode_json(payload)
        except Exception:
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                detail = to_text(error.get("message") or error.get("code"))
            elif error:
                detail = to_text(error)
            if not detail and data.get("message"):
                detail = to_text(data.get("message"))
            meta = data.get("error")
            if isinstance(meta, dict) and meta.get("metadata"):
                retry = meta["metadata"].get("retry_after_seconds")
                if retry:
                    retry_after = retry
        if not detail:
            detail = shorten(raw_text, 400)
        raise APIError(status, "HTTP %s" % status, detail=detail,
                       raw=raw_text, retry_after=retry_after)

    def _request_ssl(self, method, url, body):
        """``ssl`` + ``socket``; honours a custom ``cacert.pem`` path."""
        parsed = urlparse(url)
        host = to_text(parsed.hostname or "")
        port = parsed.port or 443
        path = parsed.path or "/"
        if parsed.query:
            path = path + "?" + parsed.query
        _context, warnings = make_ssl_context(self.cacert_path)
        self.warnings.extend(warnings)
        sock = connect_tls(host, port, self.cacert_path, self.timeout)
        try:
            lines = ["%s %s HTTP/1.1" % (method.upper(), path),
                     "Host: %s" % (parsed.netloc or host),
                     "Connection: close",
                     "Accept-Encoding: identity"]
            for key, value in self.headers().items():
                lines.append("%s: %s" % (key, value))
            if body is not None:
                lines.append("Content-Length: %d" % len(body))
            packet = to_bytes("\r\n".join(lines) + "\r\n\r\n")
            if body:
                packet += body
            sock.sendall(packet)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        response = SimpleHTTPClient._parse(b"".join(chunks), url)
        self._check_status(response.status, response.content)
        return response.status, response.content

    def _request_urllib(self, method, url, body):
        """Fallback using ``urllib2`` (with a custom CA bundle if given)."""
        request = _UrllibRequest(url, data=body, headers=self.headers())
        request.get_method = lambda: method
        handlers = []
        if self.cacert_path:
            context, warnings = make_ssl_context(self.cacert_path)
            self.warnings.extend(warnings)
            if _HTTPSHandler is not None:
                handlers.append(_HTTPSHandler(context))
        opener = _build_opener(*handlers)
        try:
            response = opener.open(request, timeout=self.timeout)
            payload = response.read()
            status = getattr(response, "getcode", lambda: 200)()
        except HTTPError as exc:
            status = exc.code
            payload = exc.read()
            self._check_status(status, payload)
            raise
        except URLError as exc:
            # urllib2 buries the useful part in ``exc.reason``
            raise IOError("urllib2: %s" % to_text(
                getattr(exc, "reason", exc)))
        self._check_status(status, payload)
        return status, payload

    # -- API surface -------------------------------------------------------
    def chat(self, messages, model=None, temperature=None, max_tokens=None):
        """POST /chat/completions and return ``(text, usage)``."""
        model = to_text(model or DEFAULT_MODEL)
        payload = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        body = utf8_json(payload)
        status, raw = self._request(
            "POST", self.url_for(CHAT_COMPLETIONS_PATH), body)
        try:
            data = decode_json(raw)
        except ValueError:
            raise APIError(status, "The reply was not valid JSON.",
                           detail=shorten(raw, 400))
        if isinstance(data, dict) and data.get("error"):
            error = data["error"]
            if isinstance(error, dict):
                detail = to_text(error.get("message") or error.get("code"))
            else:
                detail = to_text(error)
            raise APIError(status or 0, "OpenRouter returned an error.",
                           detail=detail, raw=to_text(raw))
        text = self._extract_text(data)
        if not text.strip():
            raise APIError(status, "The model returned an empty reply.",
                           detail=shorten(raw, 400))
        return text, self._extract_usage(data)

    @staticmethod
    def _extract_text(data):
        choices = []
        if isinstance(data, dict):
            choices = data.get("choices") or []
        if not choices:
            return text_type("")
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            pieces = []
            for part in content:
                if isinstance(part, dict):
                    pieces.append(to_text(part.get("text")))
                else:
                    pieces.append(to_text(part))
            content = "".join(pieces)
        content = to_text(content)
        if not content.strip():
            content = to_text(choice.get("text"))
        return content

    @staticmethod
    def _extract_usage(data):
        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
            return data["usage"]
        return {}

    def list_models(self):
        """GET /models and return the list of model dictionaries."""
        status, raw = self._request("GET", self.url_for(MODELS_PATH))
        try:
            data = decode_json(raw)
        except ValueError:
            raise APIError(status, "The model list was not valid JSON.",
                           detail=shorten(raw, 400))
        models = []
        if isinstance(data, dict):
            models = data.get("data") or []
        cleaned = []
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = to_text(model.get("id")).strip()
            if not model_id:
                continue
            cleaned.append({
                "id": model_id,
                "name": to_text(model.get("name") or model_id),
                "free": is_free_model(model_id, model.get("pricing")),
                "context": model.get("context_length"),
            })
        cleaned.sort(key=lambda entry: ("" if entry["free"] else "~",
                                        entry["id"]))
        return cleaned


if PY2:  # pragma: no cover - only exists on Python 2
    from urllib2 import Request as _UrllibRequest
    from urllib2 import build_opener as _build_opener
    from urllib2 import HTTPSHandler

    class _HTTPSHandler(HTTPSHandler):
        """``HTTPSHandler`` that verifies against a custom CA bundle."""

        def __init__(self, context):
            HTTPSHandler.__init__(self)
            self._context = context

        def https_open(self, request):  # noqa: N802 (urllib2 API)
            return self.do_open(self._connection, request)

        def _connection(self, host, **kwargs):
            import httplib

            kwargs["context"] = self._context
            return httplib.HTTPSConnection(host, **kwargs)
else:  # pragma: no cover - development convenience
    from urllib.request import Request as _UrllibRequest
    from urllib.request import build_opener as _build_opener
    from urllib.request import HTTPSHandler

    class _HTTPSHandler(HTTPSHandler):
        """``HTTPSHandler`` that verifies against a custom CA bundle.

        Python 3 spells the same hook differently: ``HTTPSConnection``
        takes the context in its constructor (as on 2.7), but the handler
        is given a ``Request`` object rather than a URL string.
        """

        def __init__(self, context):
            HTTPSHandler.__init__(self)
            self._context = context

        def https_open(self, request):  # noqa: N802 (urllib API)
            return self.do_open(self._connection, request)

        def _connection(self, host, **kwargs):
            import http.client

            kwargs["context"] = self._context
            return http.client.HTTPSConnection(host, **kwargs)


# ---------------------------------------------------------------------------
# 5. Local HTTP -> HTTPS bridge (pure Python, no Qt)
# ---------------------------------------------------------------------------

class HTTPBridge(object):
    """Forward ``http://127.0.0.1:PORT/api/v1/...`` to OpenRouter over TLS.

    This is the escape hatch for machines whose OpenSSL cannot complete a
    modern handshake: the GUI talks cleartext HTTP to localhost, and this
    bridge performs the TLS leg with the interpreter's ``ssl`` module
    (which can be pointed at a fresh ``cacert.pem``).

    Only ``openrouter.ai`` is proxied -- the bridge carries the user's API
    key, so it must never become an open relay.
    """

    def __init__(self, host="127.0.0.1", port=8787, cacert_path="",
                 log=None, upstream="https://openrouter.ai", timeout=60.0):
        self.host = to_text(host) or "127.0.0.1"
        self.port = int(port)
        self.cacert_path = resolve_cacert_path(cacert_path)
        self.upstream = upstream.rstrip("/")
        # Only the configured upstream host may be relayed: the bridge
        # carries the user's API key and must never be an open proxy.
        self.allowed_host = to_text(
            urlparse(self.upstream).hostname or "").lower()
        self.timeout = float(timeout)
        self._log = log
        self._thread = None
        self._stop_event = threading.Event()
        self._server_socket = None
        self.bound_port = None
        self.last_error = None
        self.requests_served = 0

    # -- logging -----------------------------------------------------------
    def log(self, message):
        if self._log is not None:
            try:
                self._log(to_text(message))
            except Exception:
                pass

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        """Start the accept loop on a daemon thread; non-blocking."""
        if self.is_running():
            return True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run,
                                        name="aquasnow-bridge")
        self._thread.daemon = True
        self._thread.start()
        return True

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout=2.0):
        self._stop_event.set()
        server = self._server_socket
        if server is not None:
            try:
                server.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    def run(self):
        """Blocking accept loop (also used directly by ``--bridge-only``)."""
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen(16)
            server.settimeout(0.5)
        except (socket.error, OSError) as exc:
            self.last_error = to_text(exc)
            self.log("Bridge could not listen on %s:%d -- %s"
                     % (self.host, self.port, exc))
            return
        self._server_socket = server
        try:
            self.bound_port = server.getsockname()[1]
        except Exception:
            self.bound_port = self.port
        self.log("Bridge listening on http://%s:%d -> %s"
                 % (self.host, self.bound_port, self.upstream))
        while not self._stop_event.is_set():
            try:
                client, _address = server.accept()
            except socket.timeout:
                continue
            except (socket.error, OSError):
                break
            try:
                self._serve(client)
            except Exception as exc:
                self.log("Bridge error: %s" % exc)
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        try:
            server.close()
        except Exception:
            pass
        self._server_socket = None
        self.log("Bridge stopped.")

    # -- request handling --------------------------------------------------
    def _read_request(self, client):
        """Read one HTTP request; returns ``(method, target, headers, body)``.

        Returns ``None`` when the peer sent nothing at all.
        """
        client.settimeout(self.timeout)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > 4 * 1024 * 1024:
                raise IOError("request headers too large")
        if not data:
            return None
        head, _separator, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 3:
            return ("", "", {}, b"")
        method, target = parts[0], parts[1]
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            length = 0
        body = rest
        while len(body) < length:
            chunk = client.recv(65536)
            if not chunk:
                break
            body += chunk
        return method, target, headers, body

    def resolve_target(self, target):
        """Turn a request target into an absolute URL (or ``None``)."""
        target = to_text(target)
        if target.lower().startswith(("http://", "https://")):
            upstream_url = target
        else:
            upstream_url = self.upstream + (target or "/")
        parsed = urlparse(upstream_url)
        host = to_text(parsed.hostname or "").lower()
        allowed = self.allowed_host
        if not allowed or not (host == allowed
                               or host.endswith("." + allowed)):
            return None
        return upstream_url

    def _serve(self, client):
        request = self._read_request(client)
        if request is None:
            return
        method, target, headers, body = request
        if not method:
            self._reply(client, 400, "Bad Request", "Malformed request.\n")
            return
        upstream_url = self.resolve_target(target)
        if upstream_url is None:
            # The bridge carries the user's API key: never relay it
            # anywhere but openrouter.ai.
            self._reply(client, 403, "Forbidden",
                        "AquaSnowLLM's bridge only proxies openrouter.ai "
                        "requests.\n")
            return
        self._forward(client, method, upstream_url, headers, body)
        self.requests_served += 1

    def _forward(self, client, method, url, headers, body):
        parsed = urlparse(url)
        host = to_text(parsed.hostname or "")
        port = parsed.port or 443
        path = parsed.path or "/"
        if parsed.query:
            path = path + "?" + parsed.query
        _context, warnings = make_ssl_context(self.cacert_path)
        for warning in warnings:
            self.log(warning)
        try:
            sock = connect_tls(host, port, self.cacert_path, self.timeout)
        except Exception as exc:
            # Report the TLS/DNS failure to the client instead of dropping
            # the connection: an app waiting on the bridge would otherwise
            # only see "no response".
            reason = to_text(exc)
            self.log("Bridge could not reach %s: %s" % (host, reason))
            self._reply(client, 502, "Bad Gateway",
                        "AquaSnowLLM's bridge could not reach %s over TLS.\n"
                        "\n%s\n\nTry a fresh CA bundle in Settings, or let "
                        "the app talk to OpenRouter directly."
                        % (host, reason))
            return
        try:
            outgoing = {
                "host": parsed.netloc or host,
                "connection": "close",
                "accept-encoding": "identity",
                "user-agent": USER_AGENT,
            }
            for key, value in headers.items():
                if key in ("host", "connection", "proxy-connection",
                           "content-length", "accept-encoding",
                           "transfer-encoding"):
                    continue
                outgoing[key] = value
            if body and "content-type" not in outgoing:
                outgoing["content-type"] = "application/json"
            lines = ["%s %s HTTP/1.1" % (method.upper(), path)]
            for key, value in outgoing.items():
                lines.append("%s: %s" % (key, value))
            lines.append("Content-Length: %d" % len(body or b""))
            packet = to_bytes("\r\n".join(lines) + "\r\n\r\n")
            if body:
                packet += body
            sock.sendall(packet)
            response = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response += chunk
        finally:
            try:
                sock.close()
            except Exception:
                pass

        split_at = response.find(b"\r\n\r\n")
        if split_at < 0:
            self._reply(client, 502, "Bad Gateway",
                        "The upstream server returned no data.\n")
            return
        head = response[:split_at].decode("latin-1").split("\r\n")
        payload = response[split_at + 4:]
        status_line = head[0] if head else "HTTP/1.1 502 Bad Gateway"
        status_code = 502
        try:
            status_code = int(status_line.split(" ")[1])
        except (IndexError, ValueError):
            pass
        kept = [status_line]
        for line in head[1:]:
            if ":" not in line:
                continue
            key = line.split(":", 1)[0].strip().lower()
            if key in ("connection", "transfer-encoding", "content-length",
                       "keep-alive", "proxy-authenticate"):
                continue
            kept.append(line)
        kept.append("Content-Length: %d" % len(payload))
        kept.append("Connection: close")
        client.sendall(to_bytes("\r\n".join(kept) + "\r\n\r\n") + payload)
        self.log("Bridge: %s %s -> %s" % (method, path, status_code))

    @staticmethod
    def _reply(client, status, reason, text):
        body = to_bytes(text)
        head = ("HTTP/1.1 %d %s\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n"
                % (status, reason, len(body)))
        try:
            client.sendall(to_bytes(head) + body)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 6. Qt 4 user interface
# ---------------------------------------------------------------------------

try:  # PyQt4 first: it is what the Snow Leopard recipes install.
    from PyQt4 import QtCore, QtGui
    try:
        from PyQt4 import QtNetwork  # noqa: F401
    except ImportError:
        QtNetwork = None
    QT_API = "PyQt4"
except ImportError:  # pragma: no cover - PySide fallback
    try:
        from PySide import QtCore, QtGui
        try:
            from PySide import QtNetwork  # noqa: F401
        except ImportError:
            QtNetwork = None
        QT_API = "PySide"
    except ImportError:
        QtCore = None
        QtGui = None
        QtNetwork = None
        QT_API = None


if QT_API is not None:
    Qt = QtCore.Qt
    Signal = QtCore.pyqtSignal if QT_API == "PyQt4" else QtCore.Signal
    USER_ROLE = Qt.UserRole
    ALIGN_RIGHT = Qt.AlignRight
    RICH_TEXT = Qt.RichText
    WAIT_CURSOR = Qt.WaitCursor
    ARROW_CURSOR = Qt.ArrowCursor
    KEY_RETURN = Qt.Key_Return
    KEY_ENTER = Qt.Key_Enter
    SHIFT_MODIFIER = Qt.ShiftModifier


AQUA_STYLE = (
    "QMainWindow, QDialog { background: #e9e9e9; }"
    "QListWidget { background: #ffffff; border: 1px solid #b8b8b8;"
    " selection-background-color: #3c7dd9; selection-color: white; }"
    "QTextBrowser, QTextEdit { background: #ffffff;"
    " border: 1px solid #b8b8b8; }"
    "QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {"
    " background: #ffffff; border: 1px solid #b8b8b8;"
    " border-radius: 3px; padding: 2px 4px; }"
    "QPushButton { padding: 3px 12px; min-height: 19px; }"
    "QStatusBar { background: #e9e9e9; border-top: 1px solid #b8b8b8; }"
    "QLabel#heading { font-weight: bold; }"
    "QLabel#hint { color: #6a6a6a; }"
    "QGroupBox { border: 1px solid #c4c4c4; border-radius: 4px;"
    " margin-top: 10px; padding-top: 8px; }"
)


def html_escape(value):
    text = to_text(value)
    text = text.replace("&", "&amp;").replace("<", "&lt;")
    text = text.replace(">", "&gt;").replace('"', "&quot;")
    return text


def inline_markup(text):
    """Apply the handful of inline Markdown forms worth supporting."""
    # ``code`` first, so ** inside a snippet is left alone
    pieces = []
    for index, part in enumerate(re.split(r"(`[^`]+`)", text)):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            pieces.append('<span style="font-family: Menlo, Monaco, '
                          'Courier; font-size: 12px;">%s</span>'
                          % part[1:-1])
        else:
            part = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", part)
            part = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>",
                          part)
            pieces.append(part)
    return "".join(pieces)


def format_message_body(content):
    """Turn plain text (or light Markdown) into readable, safe HTML.

    Qt's rich text engine supports a small subset of HTML 4; tables, ``pre``
    and inline styles are safe, but anything CSS3 is not.  Only the markup
    that survives on Qt 4.6 is produced here, and all text is escaped first.
    """
    text = html_escape(content)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip("\n")
        if not block:
            continue
        if block.startswith("```"):
            body = block.split("\n", 1)[1] if "\n" in block else ""
            body = re.sub(r"```\s*$", "", body)
            paragraphs.append(
                "<pre style=\"margin:4px 0; padding:6px; "
                "background-color:#f1f1f1; border:1px solid #dddddd; "
                "font-family: Menlo, Monaco, Courier; font-size: 11px; "
                "white-space: pre-wrap;\">%s</pre>" % body.strip("\n"))
            continue
        lines = []
        for line in block.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#"):
                lines.append("<b>%s</b>" % inline_markup(
                    stripped.lstrip("#").strip()))
            elif stripped.startswith(("- ", "* ")):
                lines.append("&#8226;&nbsp;%s" % inline_markup(stripped[2:]))
            elif re.match(r"^\d+\.\s", stripped):
                lines.append(inline_markup(stripped))
            else:
                lines.append(inline_markup(line))
        paragraphs.append("<p style=\"margin:0 0 6px 0;\">%s</p>"
                          % "<br>".join(lines))
    return "".join(paragraphs)


def messages_to_html(chat, notices=None):
    """Render a conversation as the HTML shown in the QTextBrowser."""
    parts = [
        '<html><head><meta http-equiv="Content-Type" '
        'content="text/html; charset=utf-8"></head><body '
        'style="font-family: \'Helvetica Neue\', Helvetica, Arial; '
        'font-size: 13px; color: #1a1a1a;">'
    ]
    messages = chat.get("messages") or []
    if not messages:
        parts.append(
            '<p style="color:#7a7a7a;">No messages yet.</p>'
            '<p style="color:#7a7a7a;">Type below, pick a model in the '
            'toolbar -- <b>openrouter/free</b> works with free API keys -- '
            'then press Return.</p>')
    for message in messages:
        role = message.get("role")
        content = format_message_body(message.get("content"))
        if role == "user":
            label, colour, background = "You", "#12233a", "#eaf1fb"
        elif role == "assistant":
            model = to_text(message.get("model") or chat.get("model"))
            label = "Assistant"
            if model:
                label = "Assistant (%s)" % shorten(model, 56)
            colour, background = "#12233a", "#f6f7f4"
        else:
            label, colour, background = "System", "#12233a", "#fbf3e6"
        stamp = friendly_time(message.get("ts"))
        meta = ""
        if stamp:
            meta = (' <span style="color:#8a8a8a; font-weight: normal; '
                    'font-size: 11px;">%s</span>' % html_escape(stamp))
        parts.append(
            '<table width="100%%" cellspacing="0" cellpadding="6" '
            'style="margin: 0 0 10px 0; background-color: %s; '
            'border: 1px solid #d8d8d8;">'
            '<tr><td><p style="margin:0 0 4px 0; color:#33475b; '
            'font-weight: bold; font-size: 11px;">%s%s</p>'
            '<div style="color:%s;">%s</div></td></tr></table>'
            % (background, html_escape(label), meta, colour, content))
    for notice in notices or []:
        parts.append(notice)
    parts.append("</body></html>")
    return "".join(parts)


def notice_html(headline, body, raw=None):
    """A friendly, selectable alert block for the transcript."""
    parts = [
        '<table width="100%" cellspacing="0" cellpadding="8" '
        'style="margin: 0 0 10px 0; background-color: #fdf2f2; '
        'border: 1px solid #e0b4b4;">'
        '<tr><td><p style="margin:0 0 6px 0; color:#9b2c2c; '
        'font-weight:bold;">AquaSnowLLM: '
        + html_escape(headline) + '</p>'
        '<p style="margin:0; color:#5a2a2a;">'
        + html_escape(body).replace("\n", "<br>") + '</p>'
    ]
    if raw:
        parts.append(
            '<p style="margin:6px 0 0 0; color:#8a8a8a; font-size: 11px;">'
            'Server said: ' + html_escape(shorten(raw, 300)) + '</p>')
    parts.append("</td></tr></table>")
    return "".join(parts)


def apply_aqua_style(app):
    """Best effort native Aqua look; a no-op where unavailable."""
    applied = None
    try:
        styles = [to_text(name) for name in QtGui.QStyleFactory.keys()]
    except Exception:
        styles = []
    for candidate in ("macintosh", "macos"):
        for name in styles:
            if to_text(name).lower() == candidate:
                try:
                    app.setStyle(name)
                    applied = name
                except Exception:
                    applied = None
                break
        if applied:
            break
    if applied and to_text(applied).lower() not in ("macintosh", "macos"):
        # On Aqua the native style already looks right; a stylesheet would
        # only fight it.  Everywhere else the light QSS keeps things legible.
        try:
            app.setStyleSheet(AQUA_STYLE)
        except Exception:
            pass
    return applied


def dialog_filename(path, filters=""):
    """Normalise QFileDialog results (PyQt4 str / PyQt5 tuple / PySide)."""
    if isinstance(path, (list, tuple)):
        path = path[0] if path else ""
    return to_text(path)


if QT_API is not None:

    class ApiWorker(QtCore.QThread):
        """Run one API call off the UI thread so Aqua never freezes."""

        signal_succeeded = Signal(object)
        signal_failed = Signal(object)

        def __init__(self, parent, job, client, payload):
            QtCore.QThread.__init__(self, parent)
            self.job = job          #: "chat" or "models" (read by the window)
            self._client = client
            self._payload = payload
            self._cancel_event = threading.Event()

        def cancel(self):
            self._cancel_event.set()

        def is_cancelled(self):
            return self._cancel_event.is_set()

        def run(self):
            try:
                if self.job == "models":
                    result = self._client.list_models()
                elif self.job == "chat":
                    result = self._client.chat(**self._payload)
                else:
                    raise ValueError("Unknown job %r" % self.job)
            except APIError as exc:
                if not self._cancel_event.is_set():
                    self.signal_failed.emit(exc)
                return
            except Exception as exc:
                if not self._cancel_event.is_set():
                    self.signal_failed.emit(APIError(0, to_text(exc)))
                return
            if not self._cancel_event.is_set():
                self.signal_succeeded.emit(result)

    class ChatView(QtGui.QTextBrowser):
        """Read-only transcript: selectable text, clickable links."""

        def __init__(self, parent=None):
            QtGui.QTextBrowser.__init__(self, parent)
            self.setOpenLinks(False)
            self.setOpenExternalLinks(False)
            self.setReadOnly(True)
            try:
                self.setTextInteractionFlags(
                    Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard |
                    Qt.LinksAccessibleByMouse)
            except AttributeError:
                pass
            try:
                self.setWordWrapMode(
                    QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
            except AttributeError:
                pass
            self.anchorClicked.connect(self._open_link)

        def _open_link(self, url):
            # PyQt4 hands over a QUrl, PyQt5 a str (and old PyQt4 str)
            if not isinstance(url, QtCore.QUrl):
                url = QtCore.QUrl(to_text(url))
            QtGui.QDesktopServices.openUrl(url)

        def show_chat(self, chat, notices=None):
            self.setHtml(messages_to_html(chat, notices))
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum())

    class SessionRow(QtGui.QWidget):
        """Two-line sidebar row: bold title over quiet metadata.

        A custom widget is used instead of ``item.setText("a\\nb")`` because
        item view delegates elide multi-line text differently across Qt 4.x
        releases; a widget renders predictably everywhere.
        """

        def __init__(self, title, subtitle, parent=None):
            QtGui.QWidget.__init__(self, parent)
            layout = QtGui.QVBoxLayout(self)
            layout.setContentsMargins(6, 3, 6, 3)
            layout.setSpacing(0)
            self.title_label = QtGui.QLabel()
            self.title_label.setToolTip(title)
            font = self.title_label.font()
            try:
                font.setBold(True)
            except AttributeError:
                pass
            self.title_label.setFont(font)
            self.subtitle_label = QtGui.QLabel(subtitle)
            sub_font = self.subtitle_label.font()
            try:
                point = sub_font.pointSizeF()
                if point and point > 0:
                    sub_font.setPointSizeF(max(7.0, point - 2.0))
                else:
                    sub_font.setPointSize(9)
            except (AttributeError, TypeError):
                sub_font.setPointSize(9)
            self.subtitle_label.setFont(sub_font)
            self.subtitle_label.setObjectName("hint")
            layout.addWidget(self.title_label)
            layout.addWidget(self.subtitle_label)
            self.set_title(title)
            self.setToolTip(title)

        def sizeHint(self):
            title = QtGui.QFontMetrics(self.title_label.font()).height()
            subtitle = QtGui.QFontMetrics(
                self.subtitle_label.font()).height()
            return QtCore.QSize(180, title + subtitle + 8)

        def minimumSizeHint(self):
            return self.sizeHint()

        def set_title(self, title):
            metrics = QtGui.QFontMetrics(self.title_label.font())
            available = max(80, self.width() - 16)
            self.title_label.setText(
                metrics.elidedText(to_text(title), Qt.ElideRight, available))

        def resizeEvent(self, event):
            QtGui.QWidget.resizeEvent(self, event)
            self.set_title(self.title_label.toolTip())


    class ComposerEdit(QtGui.QTextEdit):
        """Input box: Return sends, Shift+Return inserts a newline."""

        return_pressed = Signal()

        def __init__(self, parent=None):
            QtGui.QTextEdit.__init__(self, parent)
            self.setAcceptRichText(False)
            self.setTabChangesFocus(True)
            self.setMinimumHeight(58)
            self.setMaximumHeight(160)
            try:
                self.setPlaceholderText(
                    "Ask anything...  (Return sends, Shift+Return for a "
                    "new line)")
            except AttributeError:
                pass

        def keyPressEvent(self, event):
            if event.key() in (KEY_RETURN, KEY_ENTER):
                if event.modifiers() & SHIFT_MODIFIER:
                    QtGui.QTextEdit.keyPressEvent(self, event)
                    return
                self.return_pressed.emit()
                event.accept()
                return
            QtGui.QTextEdit.keyPressEvent(self, event)

    class SettingsDialog(QtGui.QDialog):
        """API key, endpoint/proxy, cacert and generation preferences."""

        def __init__(self, config, parent=None):
            QtGui.QDialog.__init__(self, parent)
            self.config = config
            self.setWindowTitle("AquaSnowLLM Settings")
            self.setMinimumWidth(660)
            self.resize(720, 640)
            layout = QtGui.QVBoxLayout(self)
            form = QtGui.QFormLayout()
            form.setLabelAlignment(ALIGN_RIGHT)

            self.key_edit = QtGui.QLineEdit(to_text(config.get("api_key")))
            self.key_edit.setEchoMode(QtGui.QLineEdit.Password)
            self.key_edit.setMinimumWidth(320)
            key_row = QtGui.QHBoxLayout()
            key_row.addWidget(self.key_edit, 1)
            self.show_key = QtGui.QCheckBox("Show")
            self.show_key.toggled.connect(self._toggle_key)
            key_row.addWidget(self.show_key)
            key_widget = QtGui.QWidget()
            key_widget.setLayout(key_row)
            form.addRow("API Key:", key_widget)

            link = QtGui.QLabel("<a href=\"https://openrouter.ai/keys\">"
                                "Get a free key at openrouter.ai/keys</a>")
            link.setOpenExternalLinks(True)
            link.setTextFormat(RICH_TEXT)
            form.addRow("", link)

            base_row = QtGui.QHBoxLayout()
            self.base_edit = QtGui.QLineEdit(to_text(config.get("base_url")))
            self.base_edit.setToolTip(
                "Leave empty for https://openrouter.ai/api/v1.\n"
                "On an old Mac, use the bundled bridge:\n"
                "http://127.0.0.1:8787")
            base_row.addWidget(self.base_edit, 1)
            use_bridge = QtGui.QPushButton("Use Bridge")
            use_bridge.setToolTip("Fill in the bundled local bridge address")
            use_bridge.clicked.connect(self._use_bridge)
            base_row.addWidget(use_bridge)
            base_widget = QtGui.QWidget()
            base_widget.setLayout(base_row)
            form.addRow("Custom Base URL / Proxy:", base_widget)

            self.bridge_check = QtGui.QCheckBox(
                "Start the local bridge on launch")
            self.bridge_check.setChecked(bool(config.get("bridge_autostart")))
            form.addRow("", self.bridge_check)

            port_row = QtGui.QHBoxLayout()
            self.bridge_host = QtGui.QLineEdit(config.bridge_host())
            self.bridge_host.setMaximumWidth(130)
            self.bridge_port = QtGui.QSpinBox()
            self.bridge_port.setRange(1024, 65535)
            self.bridge_port.setValue(config.bridge_port())
            port_row.addWidget(self.bridge_host)
            port_row.addWidget(QtGui.QLabel("port"))
            port_row.addWidget(self.bridge_port)
            port_row.addStretch(1)
            port_widget = QtGui.QWidget()
            port_widget.setLayout(port_row)
            form.addRow("Bridge Address:", port_widget)

            cacert_row = QtGui.QHBoxLayout()
            self.cacert_edit = QtGui.QLineEdit(to_text(config.get("cacert")))
            self.cacert_edit.setToolTip(
                "Path to a recent cacert.pem bundle, for example\n"
                "https://curl.se/ca/cacert.pem\n"
                "Needed on OS X 10.6, whose system roots are too old.")
            cacert_row.addWidget(self.cacert_edit, 1)
            browse = QtGui.QPushButton("Choose...")
            browse.clicked.connect(self._browse_cacert)
            cacert_row.addWidget(browse)
            cacert_widget = QtGui.QWidget()
            cacert_widget.setLayout(cacert_row)
            form.addRow("CA Certificate Bundle:", cacert_widget)

            self.model_edit = QtGui.QLineEdit(config.model())
            form.addRow("Default Model:", self.model_edit)

            self.temp_spin = QtGui.QDoubleSpinBox()
            self.temp_spin.setRange(0.0, 2.0)
            self.temp_spin.setSingleStep(0.1)
            self.temp_spin.setValue(config.temperature())
            form.addRow("Temperature:", self.temp_spin)

            self.tokens_spin = QtGui.QSpinBox()
            self.tokens_spin.setRange(16, 32768)
            self.tokens_spin.setValue(config.max_tokens())
            form.addRow("Max Tokens:", self.tokens_spin)

            self.timeout_spin = QtGui.QSpinBox()
            self.timeout_spin.setRange(10, 600)
            self.timeout_spin.setSuffix(" s")
            self.timeout_spin.setValue(int(config.timeout()))
            form.addRow("Network Timeout:", self.timeout_spin)

            self.headers_edit = QtGui.QLineEdit(
                to_text(config.get("extra_headers")).replace("\n", " | "))
            self.headers_edit.setToolTip(
                "Optional extra HTTP headers separated by ' | ', e.g.\n"
                "X-OpenRouter-Categories: programming")
            form.addRow("Extra Headers:", self.headers_edit)

            self.prompt_edit = QtGui.QPlainTextEdit(
                to_text(config.get("system_prompt")))
            self.prompt_edit.setMaximumHeight(70)
            try:
                self.prompt_edit.setPlaceholderText(
                    "Optional system prompt sent with every conversation")
            except AttributeError:
                pass
            form.addRow("System Prompt:", self.prompt_edit)

            layout.addLayout(form)

            hint = QtGui.QLabel(
                "Stored locally in %s. The API key is written readable by "
                "your account only." % config.data_dir)
            hint.setWordWrap(True)
            hint.setObjectName("hint")
            layout.addWidget(hint)

            buttons = QtGui.QDialogButtonBox(
                QtGui.QDialogButtonBox.Save | QtGui.QDialogButtonBox.Cancel)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def _toggle_key(self, show):
            self.key_edit.setEchoMode(
                QtGui.QLineEdit.Normal if show
                else QtGui.QLineEdit.Password)

        def _use_bridge(self):
            self.base_edit.setText("http://%s:%d"
                                   % (to_text(self.bridge_host.text()).strip()
                                      or "127.0.0.1",
                                      int(self.bridge_port.value())))
            self.bridge_check.setChecked(True)

        def _browse_cacert(self):
            path = dialog_filename(QtGui.QFileDialog.getOpenFileName(
                self, "Select cacert.pem", to_text(self.cacert_edit.text()),
                "PEM files (*.pem *.crt);;All files (*)"))
            if path:
                self.cacert_edit.setText(path)

        def values(self):
            headers = to_text(self.headers_edit.text()).replace(" | ", "\n")
            return {
                "api_key": to_text(self.key_edit.text()).strip(),
                "base_url": to_text(self.base_edit.text()).strip(),
                "cacert": to_text(self.cacert_edit.text()).strip(),
                "model": (to_text(self.model_edit.text()).strip()
                          or DEFAULT_MODEL),
                "temperature": float(self.temp_spin.value()),
                "max_tokens": int(self.tokens_spin.value()),
                "timeout": int(self.timeout_spin.value()),
                "system_prompt": to_text(self.prompt_edit.toPlainText()),
                "bridge_autostart": bool(self.bridge_check.isChecked()),
                "bridge_host": (to_text(self.bridge_host.text()).strip()
                                or "127.0.0.1"),
                "bridge_port": int(self.bridge_port.value()),
                "extra_headers": headers,
            }

    class BridgeThread(QtCore.QThread):
        """Qt wrapper around :class:`HTTPBridge` (keeps the UI thread free)."""

        signal_log = Signal(object)

        def __init__(self, host, port, cacert_path, parent=None):
            QtCore.QThread.__init__(self, parent)
            self.bridge = HTTPBridge(host, port, cacert_path,
                                     log=self._log_message)
            if parent is not None and hasattr(parent, "_bridge_message"):
                self.signal_log.connect(parent._bridge_message)

        def _log_message(self, message):
            # emitted from the bridge's worker thread; Qt queues the call on
            # the receiver's thread, so the UI update stays on the GUI thread
            self.signal_log.emit(to_text(message))

        def run(self):
            self.bridge.run()

        def stop(self):
            self.bridge.stop()
            self.wait(2000)

        def bound_port(self):
            return self.bridge.bound_port or self.bridge.port

        def last_error(self):
            return self.bridge.last_error

    class BridgeClient(object):
        """``LanguageModel`` transport that talks to :class:`HTTPBridge`.

        ``LanguageModel`` hands over the *upstream* URL it would have
        fetched; only its path is kept, because the bridge rewrites the
        target against its own configured upstream (and refuses any host
        that is not OpenRouter).
        """

        def __init__(self, base_url):
            self.base_url = to_text(base_url).rstrip("/")

        def request(self, method, url, body, headers, timeout):
            parsed = urlparse(to_text(url))
            path = parsed.path or "/"
            if parsed.query:
                path = path + "?" + parsed.query
            if not path.startswith("/"):
                path = "/" + path
            client = SimpleHTTPClient(timeout=timeout)
            response = client.request(method, self.base_url + path,
                                      body, headers)
            return response.status, response.content

    class MainWindow(QtGui.QMainWindow):
        """Sessions on the left, conversation on the right."""

        def __init__(self, config=None, store=None):
            QtGui.QMainWindow.__init__(self)
            self.config = config or Config()
            self.store = store or ChatStore(self.config.data_dir)
            self.current_id = None
            self.worker = None
            self.orphans = []
            self.bridge = None
            self.bridge_log = []
            self._model_ids = []
            self._last_models = None
            self._sending = False
            self._pending = {}
            self._notices = {}
            self._cacert_hinted = False
            self.setWindowTitle(APP_NAME)
            self.resize(1000, 700)
            self._build_actions()
            self._build_menus()
            self._build_toolbar()
            self._build_central()
            self._build_statusbar()
            self._populate_models()
            self._select_initial_chat()
            self._restore_bridge_if_needed()
            if self.store.error:
                self.set_status(self.store.error, warning=True)
            elif self.config.error:
                self.set_status(self.config.error, warning=True)
            elif getattr(self.config, "corrupt", False):
                self.set_status(
                    "config.json was damaged; settings were salvaged where "
                    "possible -- check Settings.", warning=True)
            else:
                self.set_status(
                    "Ready. %s -- Return sends, Shift+Return adds a line."
                    % self.config.model())

        # -- construction --------------------------------------------------
        def _build_actions(self):
            self.action_new = QtGui.QAction("New Chat", self)
            self.action_new.setShortcut(QtGui.QKeySequence.New)
            self.action_new.triggered.connect(self.new_chat)

            self.action_delete = QtGui.QAction("Delete Chat", self)
            self.action_delete.triggered.connect(self.delete_chat)

            self.action_export = QtGui.QAction("Export Chat...", self)
            self.action_export.triggered.connect(self.export_chat)

            self.action_close = QtGui.QAction("Close Window", self)
            self.action_close.setShortcut(QtGui.QKeySequence.Close)
            self.action_close.triggered.connect(self.close)

            self.action_settings = QtGui.QAction("Settings...", self)
            self.action_settings.setShortcut(QtGui.QKeySequence("Ctrl+,"))
            self.action_settings.triggered.connect(self.open_settings)

            self.action_refresh = QtGui.QAction("Refresh Models", self)
            self.action_refresh.setShortcut(QtGui.QKeySequence("Ctrl+R"))
            self.action_refresh.triggered.connect(self.refresh_models)

            self.action_send = QtGui.QAction("Send Message", self)
            self.action_send.setShortcut(QtGui.QKeySequence("Ctrl+Return"))
            self.action_send.triggered.connect(self.send_message)

            self.action_cancel = QtGui.QAction("Stop Request", self)
            self.action_cancel.setEnabled(False)
            self.action_cancel.triggered.connect(self.cancel_request)

            self.action_bridge_start = QtGui.QAction("Start Bridge", self)
            self.action_bridge_start.triggered.connect(self.restart_bridge)

            self.action_bridge_stop = QtGui.QAction("Stop Bridge", self)
            self.action_bridge_stop.triggered.connect(self.stop_bridge)

            self.action_data_folder = QtGui.QAction("Open Data Folder", self)
            self.action_data_folder.triggered.connect(self.open_data_folder)

            self.action_about = QtGui.QAction("About AquaSnowLLM", self)
            self.action_about.triggered.connect(self.show_about)

            self.action_copy = QtGui.QAction("Copy", self)
            self.action_copy.setShortcut(QtGui.QKeySequence.Copy)
            self.action_copy.triggered.connect(self._copy_selection)

            self.action_select_all = QtGui.QAction("Select All", self)
            self.action_select_all.setShortcut(QtGui.QKeySequence.SelectAll)
            self.action_select_all.triggered.connect(self._select_all)

            self.action_paste = QtGui.QAction("Paste", self)
            self.action_paste.setShortcut(QtGui.QKeySequence.Paste)
            self.action_paste.triggered.connect(self._paste)

        def _build_menus(self):
            menu = self.menuBar()
            file_menu = menu.addMenu("File")
            file_menu.addAction(self.action_new)
            file_menu.addAction(self.action_export)
            file_menu.addSeparator()
            file_menu.addAction(self.action_close)

            edit_menu = menu.addMenu("Edit")
            edit_menu.addAction(self.action_copy)
            edit_menu.addAction(self.action_paste)
            edit_menu.addAction(self.action_select_all)

            chat_menu = menu.addMenu("Chat")
            chat_menu.addAction(self.action_send)
            chat_menu.addAction(self.action_cancel)
            chat_menu.addSeparator()
            chat_menu.addAction(self.action_refresh)
            chat_menu.addAction(self.action_settings)

            tools_menu = menu.addMenu("Tools")
            tools_menu.addAction(self.action_bridge_start)
            tools_menu.addAction(self.action_bridge_stop)
            tools_menu.addAction(self.action_data_folder)

            help_menu = menu.addMenu("Help")
            help_menu.addAction(self.action_about)

        def _build_toolbar(self):
            bar = self.addToolBar("Main")
            bar.setMovable(False)
            try:
                bar.setToolButtonStyle(Qt.ToolButtonTextOnly)
            except AttributeError:
                pass

            bar.addWidget(QtGui.QLabel("Model:"))
            self.model_combo = QtGui.QComboBox()
            self.model_combo.setEditable(True)
            self.model_combo.setMinimumWidth(330)
            self.model_combo.setInsertPolicy(QtGui.QComboBox.NoInsert)
            try:
                completer = self.model_combo.completer()
            except AttributeError:
                completer = None
            if completer is not None:
                completer.setCaseSensitivity(Qt.CaseInsensitive)
            self.model_combo.currentIndexChanged.connect(self._model_chosen)
            self.model_combo.editTextChanged.connect(self._model_edited)
            bar.addWidget(self.model_combo)

            self.free_check = QtGui.QCheckBox("Free Only")
            self.free_check.setChecked(self.config.free_only())
            self.free_check.setToolTip(
                "List only models ending in :free (recommended with free "
                "API keys)")
            self.free_check.toggled.connect(self._free_only_toggled)
            bar.addWidget(self.free_check)

            self.refresh_button = QtGui.QPushButton("Refresh Models")
            self.refresh_button.clicked.connect(self.refresh_models)
            bar.addWidget(self.refresh_button)

            spacer = QtGui.QWidget()
            spacer.setSizePolicy(QtGui.QSizePolicy.Expanding,
                                 QtGui.QSizePolicy.Preferred)
            bar.addWidget(spacer)

            settings_button = QtGui.QPushButton("Settings")
            settings_button.clicked.connect(self.open_settings)
            bar.addWidget(settings_button)

        def _build_central(self):
            splitter = QtGui.QSplitter(Qt.Horizontal, self)

            left = QtGui.QWidget(splitter)
            left_layout = QtGui.QVBoxLayout(left)
            left_layout.setContentsMargins(6, 6, 6, 6)
            heading = QtGui.QLabel("Chats")
            heading.setObjectName("heading")
            left_layout.addWidget(heading)
            self.session_list = QtGui.QListWidget()
            self.session_list.setAlternatingRowColors(True)
            self.session_list.currentRowChanged.connect(self._session_selected)
            left_layout.addWidget(self.session_list, 1)
            buttons = QtGui.QHBoxLayout()
            new_button = QtGui.QPushButton("+ New Chat")
            new_button.clicked.connect(self.new_chat)
            delete_button = QtGui.QPushButton("Delete")
            delete_button.clicked.connect(self.delete_chat)
            buttons.addWidget(new_button, 1)
            buttons.addWidget(delete_button, 1)
            left_layout.addLayout(buttons)
            splitter.addWidget(left)

            right = QtGui.QWidget(splitter)
            right_layout = QtGui.QVBoxLayout(right)
            right_layout.setContentsMargins(6, 6, 6, 6)
            self.chat_view = ChatView(right)
            right_layout.addWidget(self.chat_view, 1)

            self.composer = ComposerEdit(right)
            right_layout.addWidget(self.composer)

            bottom = QtGui.QHBoxLayout()
            self.hint_label = QtGui.QLabel(
                "Return sends. Shift+Return starts a new line.")
            self.hint_label.setObjectName("hint")
            bottom.addWidget(self.hint_label, 1)
            self.stop_button = QtGui.QPushButton("Stop")
            self.stop_button.setEnabled(False)
            self.stop_button.clicked.connect(self.cancel_request)
            bottom.addWidget(self.stop_button)
            self.send_button = QtGui.QPushButton("Send")
            self.send_button.setDefault(True)
            self.send_button.setMinimumWidth(80)
            self.send_button.clicked.connect(self.send_message)
            bottom.addWidget(self.send_button)
            right_layout.addLayout(bottom)
            splitter.addWidget(right)

            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            splitter.setSizes([240, 760])
            self.setCentralWidget(splitter)
            self.composer.return_pressed.connect(self.send_message)

        def _build_statusbar(self):
            self.status_label = QtGui.QLabel("")
            self.statusBar().addWidget(self.status_label, 1)

        # -- models --------------------------------------------------------
        def _default_model_entries(self):
            return [{"id": model_id, "name": model_id,
                     "free": is_free_model(model_id)}
                    for model_id in DEFAULT_MODELS]

        def _populate_models(self, models=None, keep_selection=True):
            # ``config.model()`` is the source of truth: it is updated by
            # both dropdown picks and hand-typed ids, and it cannot be
            # clobbered by the combo box's "first item" default.
            keep = self.config.model() if keep_selection else ""
            if models is None:
                entries = self._default_model_entries()
            else:
                entries = models
            free_only = self.config.free_only()
            self.model_combo.blockSignals(True)
            try:
                self.model_combo.clear()
                ids = []
                seen = set()
                for entry in entries:
                    model_id = to_text(entry.get("id")).strip()
                    free = entry.get("free")
                    if free is None:
                        free = is_free_model(model_id)
                    if not model_id or model_id in seen:
                        continue
                    if free_only and not free:
                        continue
                    seen.add(model_id)
                    ids.append(model_id)
                    label = model_id
                    name = to_text(entry.get("name"))
                    if name and name != model_id:
                        label = "%s  (%s)" % (model_id, shorten(name, 40))
                    self.model_combo.addItem(label, model_id)
                if not ids:
                    for model_id in DEFAULT_MODELS:
                        if not free_only or is_free_model(model_id):
                            ids.append(model_id)
                            self.model_combo.addItem(model_id, model_id)
                self._model_ids = ids
                target = keep or self.config.model()
                index = self.model_combo.findData(target)
                if index < 0:
                    index = self.model_combo.findText(target)
                if index >= 0:
                    self.model_combo.setCurrentIndex(index)
                else:
                    self.model_combo.setEditText(target)
            finally:
                self.model_combo.blockSignals(False)
            self._update_model_hint()

        def current_model(self):
            """The model id currently shown, even for typed custom ids."""
            text = to_text(self.model_combo.currentText())
            index = self.model_combo.currentIndex()
            if index >= 0:
                label = to_text(self.model_combo.itemText(index))
                if label == text:
                    data = variant_to_text(self.model_combo.itemData(index))
                    if data:
                        return data
            if "  (" in text:
                text = text.split("  (", 1)[0]
            return text.strip() or self.config.model() or DEFAULT_MODEL

        def _update_model_hint(self):
            model = self.config.model()
            if model == "openrouter/free":
                tip = ("openrouter/free lets OpenRouter pick a free model "
                       "automatically: no credit needed.")
            elif model.endswith(":free"):
                tip = "Free model: requests cost $0."
            else:
                tip = ("This model is not free; a free API key will get "
                       "HTTP 402.")
            self.model_combo.setToolTip(tip)
            index = self.model_combo.findData(model)
            if index >= 0:
                self.model_combo.setItemData(index, tip, Qt.ToolTipRole)

        def _model_chosen(self, index):
            if index is None or int(index) < 0:
                return
            model_id = variant_to_text(self.model_combo.itemData(int(index)))
            if not model_id:
                return
            self.config["model"] = model_id
            self.config.save()
            self._update_model_hint()
            self.set_status("Model set to %s" % model_id)

        def _model_edited(self, text_value):
            value = to_text(text_value).strip()
            if not value:
                return
            if "  (" in value:
                value = value.split("  (", 1)[0].strip()
            if value != self.config.model():
                self.config["model"] = value
                self.config.save()
                self._update_model_hint()

        def _free_only_toggled(self, checked):
            self.config["free_only"] = bool(checked)
            self.config.save()
            self._populate_models(self._last_models, keep_selection=True)

        def refresh_models(self):
            client, error = self._make_client(require_key=False)
            if error is not None:
                self.add_notice("cannot refresh models", error)
                return
            self.refresh_button.setEnabled(False)
            self.action_refresh.setEnabled(False)
            self.set_status("Refreshing the model list from OpenRouter...")
            self._start_worker("models", client, {})

        def _models_ready(self, models):
            self._last_models = models
            self.refresh_button.setEnabled(True)
            self.action_refresh.setEnabled(True)
            free_count = len([model for model in models if model.get("free")])
            self._populate_models(models, keep_selection=True)
            self.set_status("%d models loaded (%d free)."
                            % (len(models), free_count))

        def _models_failed(self, error):
            self.refresh_button.setEnabled(True)
            self.action_refresh.setEnabled(True)
            self.add_notice("could not refresh models", error)
            self.set_status("Model refresh failed.", warning=True)

        # -- sessions ------------------------------------------------------
        def _select_initial_chat(self):
            if not self.store.chats:
                self.store.new_chat(persist=False)
            self.current_id = self.store.chats[0]["id"]
            self.refresh_session_list()
            self._show_chat(self.store.chats[0])

        def refresh_session_list(self):
            self.session_list.blockSignals(True)
            try:
                self.session_list.clear()
                select_row = 0
                for row, chat in enumerate(self.store.chats):
                    title = shorten(chat.get("title") or "New Chat",
                                    TITLE_MAX + 8)
                    count = len(chat.get("messages") or [])
                    when = friendly_time(chat.get("updated"))
                    subtitle = "%d message%s" % (count,
                                                 "" if count == 1 else "s")
                    if when:
                        subtitle = "%s - %s" % (when, subtitle)
                    row_widget = SessionRow(title, subtitle,
                                            self.session_list)
                    item = QtGui.QListWidgetItem()
                    item.setData(USER_ROLE, chat.get("id"))
                    # a QListWidget does not auto-size rows for item widgets
                    item.setSizeHint(row_widget.sizeHint())
                    item.setToolTip(text_type(chat.get("title") or ""))
                    self.session_list.addItem(item)
                    self.session_list.setItemWidget(item, row_widget)
                    if chat.get("id") == self.current_id:
                        select_row = row
                if self.session_list.count():
                    self.session_list.setCurrentRow(select_row)
            finally:
                self.session_list.blockSignals(False)

        def _session_selected(self, row):
            if row is None or int(row) < 0:
                return
            if int(row) >= self.session_list.count():
                return
            item = self.session_list.item(int(row))
            chat = self.store.find(variant_to_text(item.data(USER_ROLE)))
            if chat is None:
                return
            self.current_id = chat["id"]
            self._show_chat(chat)
            self.composer.setFocus()

        def _current_chat(self):
            chat = self.store.find(self.current_id)
            if chat is None:
                if not self.store.chats:
                    self.store.new_chat(persist=False)
                chat = self.store.chats[0]
                self.current_id = chat["id"]
            return chat

        def _show_chat(self, chat):
            self.chat_view.show_chat(chat, self._notices.get(chat.get("id")))

        def new_chat(self):
            if self._sending:
                self.set_status("Wait for the answer, or press Stop first.",
                                warning=True)
                return
            chat = self.store.new_chat(model=self.config.model())
            self.current_id = chat["id"]
            self._notices.pop(chat["id"], None)
            self.refresh_session_list()
            self._show_chat(chat)
            self.composer.setFocus()
            self.set_status("New chat. Model: %s" % self.config.model())

        def delete_chat(self):
            chat = self.store.find(self.current_id)
            if chat is None:
                return
            title = shorten(chat.get("title") or "this chat", 60)
            answer = QtGui.QMessageBox.question(
                self, "Delete Chat",
                "Delete \"%s\"?\n\nThis cannot be undone." % title,
                QtGui.QMessageBox.Yes | QtGui.QMessageBox.No,
                QtGui.QMessageBox.No)
            if answer != QtGui.QMessageBox.Yes:
                return
            self.cancel_request(silent=True)
            self.store.remove(self.current_id)
            self.current_id = self.store.chats[0]["id"]
            self.refresh_session_list()
            self._show_chat(self._current_chat())
            self.set_status("Chat deleted.")

        def export_chat(self):
            chat = self._current_chat()
            default = re.sub(r"[^A-Za-z0-9 _.-]", "",
                             shorten(chat.get("title") or "chat", 40))
            default = default.strip() or "chat"
            path = dialog_filename(QtGui.QFileDialog.getSaveFileName(
                self, "Export Chat",
                os.path.join(os.path.expanduser("~"), default + ".md"),
                "Markdown (*.md);;Text files (*.txt)"))
            if not path:
                return
            lines = ["# %s" % to_text(chat.get("title") or "Chat"), ""]
            for message in chat.get("messages") or []:
                role = message.get("role")
                label = {"user": "You", "assistant": "Assistant"}.get(
                    role, "System")
                if role == "assistant" and message.get("model"):
                    label = "Assistant (%s)" % message["model"]
                lines.append("## %s" % label)
                lines.append("")
                lines.append(to_text(message.get("content")))
                lines.append("")
            if not atomic_write_text(path, "\n".join(lines), mode=0o644):
                QtGui.QMessageBox.warning(
                    self, APP_NAME,
                    "Could not write that file. Please pick another "
                    "location.")
            else:
                self.set_status("Exported to %s" % path)

        # -- client plumbing -----------------------------------------------
        def _make_client(self, require_key=True):
            key = self.config.api_key()
            local_proxy = self.config.uses_bridge()
            if require_key and not key and not local_proxy:
                return None, APIError(
                    401, "No API key configured.",
                    detail="Open Settings (Cmd+,) and paste a free key from "
                           "https://openrouter.ai/keys. Free keys work with "
                           ":free models and openrouter/free.")
            for warning in self.config.base_url_warnings():
                self.set_status(warning, warning=True)
            bridge = None
            if self.config.uses_bridge():
                bridge = BridgeClient(self.config.base_url())
            client = LanguageModel(
                key,
                base_url=self.config.base_url(),
                cacert_path=resolve_cacert_path(self.config.get("cacert")),
                timeout=self.config.timeout(),
                bridge=bridge,
                extra_headers=self.config.extra_headers())
            if not self._cacert_hinted and not self.config.get("cacert") \
                    and hasattr(ssl, "OPENSSL_VERSION"):
                self._cacert_hinted = True
                version = to_text(getattr(ssl, "OPENSSL_VERSION", ""))
                if "0.9.8" in version or "0.9.7" in version:
                    self.set_status(
                        "Old OpenSSL (%s): set a recent cacert.pem in "
                        "Settings, or use the bundled bridge." % version,
                        warning=True)
            return client, None

        def _request_bridge(self):
            """The bridge the GUI should talk to, if any."""
            if self.bridge is not None and self.bridge.isRunning():
                return self.bridge.bridge
            return None

        def _start_worker(self, job, client, payload):
            if self.worker is not None and self.worker.isRunning():
                self.set_status("A request is already running. Press Stop "
                                "to abandon it.", warning=True)
                return
            self._reap_orphans()
            worker = ApiWorker(self, job, client, payload)
            if job == "models":
                worker.signal_succeeded.connect(self._models_ready)
                worker.signal_failed.connect(self._models_failed)
            else:
                worker.signal_succeeded.connect(self._chat_ready)
                worker.signal_failed.connect(self._chat_failed)
            worker.finished.connect(self._worker_finished)
            self.worker = worker
            worker.start()

        def _reap_orphans(self):
            alive = []
            for worker in self.orphans:
                if worker.isRunning():
                    alive.append(worker)
                else:
                    worker.deleteLater()
            self.orphans = alive

        def _worker_finished(self):
            """Re-enable the UI once a worker ends (models or chat)."""
            sender = self.sender()
            if sender is not None and sender is not self.worker:
                return
            job = getattr(sender, "job", None) if sender is not None else None
            if self.worker is sender:
                self.worker = None
            # A model refresh must never un-block a chat that is still
            # waiting for its answer (and vice versa).
            if job == "chat" or not self._sending:
                self._set_busy(False)

        def _set_busy(self, busy, message=None):
            self._sending = bool(busy)
            self.send_button.setEnabled(not busy)
            self.stop_button.setEnabled(busy)
            self.action_cancel.setEnabled(busy)
            self.composer.setReadOnly(busy)
            self.setCursor(WAIT_CURSOR if busy else ARROW_CURSOR)
            if busy and message:
                self.set_status(message)

        # -- sending -------------------------------------------------------
        def _conversation_payload(self, chat):
            messages = []
            system_prompt = to_text(self.config.get("system_prompt")).strip()
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            for message in chat.get("messages") or []:
                role = message.get("role")
                content = to_text(message.get("content"))
                if role in ("user", "assistant", "system") and content:
                    messages.append({"role": role, "content": content})
            return messages

        def send_message(self):
            if self._sending:
                return
            text = transliterate(to_text(self.composer.toPlainText())).strip()
            if not text:
                return
            chat = self._current_chat()
            model = self.config.model()
            if not is_free_model(model):
                answer = QtGui.QMessageBox.question(
                    self, APP_NAME,
                    "%s is not a free model. With a free API key the "
                    "request will fail with HTTP 402.\n\nSend anyway?"
                    % model,
                    QtGui.QMessageBox.Yes | QtGui.QMessageBox.No,
                    QtGui.QMessageBox.No)
                if answer != QtGui.QMessageBox.Yes:
                    self.composer.setFocus()
                    return
            client, error = self._make_client()
            if error is not None:
                # keep the draft: nothing the user typed should be lost
                self.add_notice("cannot send", error)
                if error.status == 401:
                    self.set_status(
                        "No API key yet -- press Settings (Cmd+,) and paste "
                        "a free key from openrouter.ai/keys.", warning=True)
                else:
                    self.set_status(error.friendly(model).split("\n")[0],
                                    warning=True)
                return

            self.store.append_message(chat, "user", text, model=model)
            chat["model"] = model
            self.store.touch(chat)
            self.store.save()
            self.composer.clear()
            self._notices.pop(chat["id"], None)
            self._show_chat(chat)
            self.refresh_session_list()

            payload = {
                "messages": self._conversation_payload(chat),
                "model": model,
                "temperature": self.config.temperature(),
                "max_tokens": self.config.max_tokens(),
            }
            self._pending = {"chat_id": chat["id"], "model": model,
                             "started": time.time()}
            self._set_busy(True, "Asking %s ..." % shorten(model, 50))
            self._start_worker("chat", client, payload)

        def _chat_ready(self, result):
            text, usage = result
            pending = self._pending or {}
            chat = self.store.find(pending.get("chat_id") or self.current_id)
            if chat is None:
                return
            self.store.append_message(chat, "assistant",
                                      transliterate(to_text(text)),
                                      model=pending.get("model"))
            self.store.touch(chat)
            saved = self.store.save()
            if chat.get("id") == self.current_id:
                self._show_chat(chat)
            self.refresh_session_list()
            elapsed = time.time() - pending.get("started", time.time())
            note = "Answer received in %s." % format_elapsed(elapsed)
            if isinstance(usage, dict) and usage.get("total_tokens"):
                note += " %s tokens." % usage.get("total_tokens")
            if not saved:
                note = self.store.error
                self.set_status(note, warning=True)
            else:
                self.set_status(note)

        def _chat_failed(self, error):
            pending = self._pending or {}
            chat = self.store.find(pending.get("chat_id") or self.current_id)
            if chat is not None:
                self._show_chat(chat)
            self.add_notice("request failed", error)
            self.set_status("Request failed (HTTP %s)."
                            % (error.status or "no response"), warning=True)

        def cancel_request(self, silent=False):
            worker = self.worker
            if worker is None:
                return
            try:
                worker.cancel()
            except Exception:
                pass
            for signal in (worker.signal_succeeded, worker.signal_failed,
                           worker.finished):
                try:
                    signal.disconnect()
                except (TypeError, RuntimeError):
                    pass
            self.orphans.append(worker)
            self.worker = None
            self._set_busy(False)
            if not silent:
                self.set_status("Request stopped. Any late answer is "
                                "ignored.", warning=True)

        def add_notice(self, headline, error, raw=None):
            """Show a friendly, selectable alert inside the transcript."""
            if isinstance(error, APIError):
                body = error.friendly(self.config.model())
                raw_text = error.raw if raw is None else raw
            else:
                body = to_text(error)
                raw_text = raw
            html = notice_html(headline, body, raw_text)
            chat_id = self.current_id
            if chat_id:
                self._notices.setdefault(chat_id, []).append(html)
            self.chat_view.moveCursor(QtGui.QTextCursor.End)
            self.chat_view.insertHtml(html)
            self.chat_view.moveCursor(QtGui.QTextCursor.End)
            bar = self.chat_view.verticalScrollBar()
            bar.setValue(bar.maximum())

        # -- settings ------------------------------------------------------
        def open_settings(self):
            dialog = SettingsDialog(self.config, self)
            if not dialog.exec_():
                return
            values = dialog.values()
            previous_port = (self.config.bridge_host(),
                             self.config.bridge_port())
            self.config.data.update(values)
            if not self.config.save():
                QtGui.QMessageBox.warning(self, APP_NAME, self.config.error)
            self.free_check.setChecked(self.config.free_only())
            self._populate_models(self._last_models, keep_selection=False)
            current_port = (self.config.bridge_host(),
                            self.config.bridge_port())
            want_bridge = (self.config.get("bridge_autostart")
                           and self.config.uses_bridge()
                           and self._bridge_is_local())
            if want_bridge:
                if previous_port != current_port or self.bridge is None:
                    self.restart_bridge()
            elif self.bridge is not None:
                self.stop_bridge()
            self.set_status("Settings saved to %s" % self.config.path)

        def open_data_folder(self):
            folder = self.config.data_dir
            if not os.path.isdir(folder):
                try:
                    os.makedirs(folder, 0o700)
                except OSError:
                    pass
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(folder))

        def show_about(self):
            text = (
                "<b>%s %s</b><br><br>"
                "A lightweight Aqua chat client for OpenRouter, written for "
                "Python 2.7 and Qt 4 so it also runs on Mac OS X 10.6 Snow "
                "Leopard.<br><br>"
                "Qt binding: %s<br>Python: %s<br>"
                "Settings and chats: %s<br><br>"
                "<a href=\"%s\">%s</a>"
                % (APP_NAME, APP_VERSION, QT_API, sys.version.split()[0],
                   html_escape(self.config.data_dir), APP_URL, APP_URL))
            QtGui.QMessageBox.about(self, "About " + APP_NAME, text)

        # -- bridge --------------------------------------------------------
        def _restore_bridge_if_needed(self):
            if self.config.get("bridge_autostart") and \
                    self.config.uses_bridge() and self._bridge_is_local():
                self.start_bridge(quiet=True)

        def _bridge_is_local(self):
            return self.config.bridge_host() in ("127.0.0.1", "localhost",
                                                 "::1")

        def start_bridge(self, quiet=False):
            if self.bridge is not None and self.bridge.isRunning():
                if not quiet:
                    self.set_status("The bridge is already running (%s)."
                                    % self.config.bridge_url())
                return
            self.bridge = BridgeThread(
                self.config.bridge_host(), self.config.bridge_port(),
                resolve_cacert_path(self.config.get("cacert")), self)
            self.bridge.signal_log.connect(self._bridge_message)
            self.bridge.start()
            self.set_status("Starting the local bridge on %s:%d ..."
                            % (self.config.bridge_host(),
                               self.config.bridge_port()))

        def stop_bridge(self):
            if self.bridge is None:
                self.set_status("The bridge is not running.")
                return
            self.bridge.stop()
            self.bridge = None
            self.set_status("Local bridge stopped.")

        def restart_bridge(self):
            if self.bridge is not None:
                self.bridge.stop()
                self.bridge = None
            self.start_bridge()

        def _bridge_message(self, message):
            message = to_text(message)
            self.bridge_log.append(message)
            if len(self.bridge_log) > 200:
                del self.bridge_log[:-200]
            self.statusBar().showMessage(message, 6000)
            self.set_status(message)

        # -- misc ----------------------------------------------------------
        def set_status(self, message, warning=False):
            self.status_label.setText(("! " if warning else "")
                                      + to_text(message))
            if warning:
                self.status_label.setStyleSheet("color: #9b2c2c;")
            else:
                self.status_label.setStyleSheet("")

        def _focus_widget(self):
            widget = QtGui.QApplication.focusWidget()
            return widget

        def _copy_selection(self):
            widget = self._focus_widget()
            for candidate in (widget, self.chat_view, self.composer):
                if candidate is not None and hasattr(candidate, "copy"):
                    candidate.copy()
                    return

        def _select_all(self):
            widget = self._focus_widget()
            if widget is self.composer:
                self.composer.selectAll()
            else:
                self.chat_view.selectAll()

        def _paste(self):
            widget = self._focus_widget()
            if widget is not None and hasattr(widget, "paste") and \
                    widget is not self.chat_view:
                widget.paste()
            else:
                self.composer.paste()

        def closeEvent(self, event):
            worker = self.worker
            if worker is not None and worker.isRunning():
                try:
                    worker.cancel()
                    worker.wait(2000)
                except Exception:
                    pass
            self._reap_orphans()
            if self.bridge is not None:
                self.bridge.stop()
                self.bridge = None
            self.store.save()
            self.config.save()
            QtGui.QMainWindow.closeEvent(self, event)


# ---------------------------------------------------------------------------
# 7. Entry point
# ---------------------------------------------------------------------------

USAGE = """\
AquaSnowLLM %s -- an Aqua chat client for OpenRouter (Python 2.7 + Qt 4)

Usage:
  python2.7 aquasnowllm.py                 start the application
  python2.7 aquasnowllm.py --version       print the version
  python2.7 aquasnowllm.py --data-dir DIR  use DIR instead of ~/.aquasnowllm
  python2.7 aquasnowllm.py --bridge-only   run the HTTP-to-HTTPS bridge only
  python2.7 aquasnowllm.py --self-test     run the offline self test

Environment:
  AQUASNOWLLM_HOME       overrides the ~/.aquasnowllm data folder
""" % APP_VERSION


def _bridge_only(argv):
    """Headless mode: forward http://127.0.0.1:PORT to OpenRouter."""
    host, port = "127.0.0.1", 8787
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--port", "-p") and index + 1 < len(argv):
            try:
                port = int(argv[index + 1])
            except ValueError:
                pass
            index += 2
            continue
        if arg == "--host" and index + 1 < len(argv):
            host = argv[index + 1]
            index += 2
            continue
        index += 1
    config = Config()
    bridge = HTTPBridge(host, port,
                        resolve_cacert_path(config.get("cacert")),
                        log=_print_line)
    bridge.start()
    if not bridge.is_running() and bridge.last_error:
        _print_line("Could not start the bridge: %s" % bridge.last_error)
        return 1
    _print_line("Bridge running at http://%s:%d%s"
                % (host, port, " (Ctrl+C to stop)"))
    _print_line("In AquaSnowLLM: Settings -> Custom Base URL = "
                "http://%s:%d" % (host, port))
    try:
        while bridge.is_running():
            time.sleep(0.3)
    except KeyboardInterrupt:
        _print_line("")
        _print_line("Stopping the bridge...")
    bridge.stop()
    return 0


def _print_line(message):
    sys.stdout.write(to_bytes(message) + b"\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _self_test():
    """Offline sanity checks (``--self-test`` and tools/selftest.py)."""
    failures = []

    def check(label, condition):
        if condition:
            print("  ok   %s" % label)
        else:
            failures.append(label)
            print("  FAIL %s" % label)

    print("AquaSnowLLM %s self test (Python %s, Qt binding: %s)"
          % (APP_VERSION, sys.version.split()[0], QT_API or "none"))
    check("to_text keeps astral characters",
          to_text(u"\U0001f600").encode("unicode_escape") == b"\\U0001f600")
    check("to_bytes round trip",
          to_bytes(to_text(u"caf\u00e9")) == b"caf\xc3\xa9")
    check("utf8_json escapes astral characters",
          b"\\ud83d\\ude00" in utf8_json({"t": u"\U0001f600"}))
    check("decode_json survives surrogate pairs",
          decode_json(b'{"t": "\\ud83d\\ude00"}')[text_type("t")] ==
          u"\U0001f600")
    check("transliterate folds smart quotes",
          transliterate(u"\u201chi\u201d \u2013 there\u2026") ==
          "\"hi\" - there...")
    check("is_free_model",
          is_free_model("qwen/qwen3.8-27b:free") and
          is_free_model("openrouter/free") and
          not is_free_model("openai/gpt-4o"))
    check("is_free_model reads pricing",
          is_free_model("x/y", {"prompt": "0", "completion": "0"}))
    check("normalize_base_url adds /api/v1",
          normalize_base_url("https://openrouter.ai")[0] ==
          "https://openrouter.ai/api/v1")
    check("normalize_base_url keeps proxy ports",
          normalize_base_url("http://127.0.0.1:8787")[0] ==
          "http://127.0.0.1:8787/api/v1")
    check("normalize_base_url rejects odd schemes",
          normalize_base_url("ftp://example.com")[0] ==
          OPENROUTER_BASE_URL)
    check("cacert discovery prefers the data folder", _selftest_cacert())
    check("split_api_base",
          split_api_base("https://openrouter.ai/api/v1") ==
          ("https://openrouter.ai", "/api/v1"))
    check("ChatStore round trip", _selftest_store())
    check("Config salvages a damaged file", _selftest_config())
    check("messages_to_html escapes", _selftest_html())
    check("HTTP parsing", _selftest_http())
    check("bridge refuses foreign hosts", _selftest_bridge())
    print("")
    if failures:
        print("%d check(s) failed." % len(failures))
        return 1
    print("All checks passed.")
    return 0


def _selftest_cacert():
    """A bundle in the data folder must be picked up automatically."""
    import shutil
    folder = tempfile.mkdtemp(prefix="aquasnow-ca-")
    previous = os.environ.get("AQUASNOWLLM_HOME")
    try:
        os.environ["AQUASNOWLLM_HOME"] = folder
        bundle = os.path.join(folder, "cacert.pem")
        handle = codecs.open(bundle, "w", "ascii")
        try:
            handle.write("-----BEGIN CERTIFICATE-----\n")
        finally:
            handle.close()
        found = resolve_cacert_path("")
        return found == bundle and resolve_cacert_path("~/x.pem") == \
            os.path.expanduser("~/x.pem")
    finally:
        if previous is None:
            os.environ.pop("AQUASNOWLLM_HOME", None)
        else:
            os.environ["AQUASNOWLLM_HOME"] = previous
        shutil.rmtree(folder, ignore_errors=True)


def _selftest_store():
    import shutil

    folder = tempfile.mkdtemp(prefix="aquasnow-")
    try:
        store = ChatStore(folder)
        chat = store.new_chat(persist=True)
        store.append_message(chat, "user", u"h\u00e9llo \u201cworld\u201d")
        store.append_message(chat, "assistant", u"hi \U0001f600")
        store.save()
        again = ChatStore(folder)
        first = again.chats[0]
        if len(first["messages"]) != 2:
            return False
        if first["messages"][1]["content"].encode("unicode_escape") != \
                b"hi \\U0001f600":
            return False
        if first["title"] != u"h\u00e9llo \u201cworld\u201d":
            return False
        again.remove(first["id"])
        return len(again.chats) == 1
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _selftest_config():
    import shutil

    folder = tempfile.mkdtemp(prefix="aquasnow-cfg-")
    try:
        broken = ('{"api_key": "sk-or-v1-xyz", "model": "a/b:free",'
                  ' "temperature": 0.4,, broken')
        handle = codecs.open(os.path.join(folder, "config.json"), "w",
                             "utf-8")
        try:
            handle.write(broken)
        finally:
            handle.close()
        config = Config(folder)
        if not config.corrupt:
            return False
        if config.api_key() != "sk-or-v1-xyz":
            return False
        if config.model() != "a/b:free":
            return False
        # a healthy file loads normally and saves atomically
        config.save()
        reloaded = Config(folder)
        return (reloaded.api_key() == "sk-or-v1-xyz"
                and not reloaded.corrupt)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _selftest_html():
    chat = {"model": "m", "messages": [
        {"role": "user", "content": "<script>x & y</script>", "ts": ""}]}
    html = messages_to_html(chat)
    return "&lt;script&gt;" in html and "<script>" not in html


def _selftest_http():
    raw = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
           b"Content-Length: 5\r\n\r\nhello")
    response = SimpleHTTPClient._parse(raw, "http://127.0.0.1/")
    if response.status != 200 or response.content != b"hello":
        return False
    chunked = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"5\r\nhello\r\n0\r\n\r\n")
    response = SimpleHTTPClient._parse(chunked, "http://127.0.0.1/")
    return response.content == b"hello"


def _selftest_bridge():
    """The bridge must proxy openrouter.ai and refuse everything else."""
    bridge = HTTPBridge("127.0.0.1", 0, "", log=None)
    bridge.log = lambda message: None

    # relative targets are resolved against the upstream host
    if bridge.resolve_target("/api/v1/models") != \
            "https://openrouter.ai/api/v1/models":
        return False
    if bridge.resolve_target("https://openrouter.ai/x") is None:
        return False
    # ...and this is the security guard: no open relay
    if bridge.resolve_target("http://evil.example/api/v1/models") is not None:
        return False
    if bridge.resolve_target(
            "https://openrouter.ai.evil.example/api/v1/models") is not None:
        return False

    # a request for a foreign host is answered with 403, never forwarded
    class _FakePeer(object):
        def __init__(self):
            self.replied = b""
            self.queue = [b"GET http://evil.example/x HTTP/1.1\r\n"
                          b"Host: evil.example\r\n\r\n"]

        def settimeout(self, _value):
            pass

        def recv(self, _size):
            return self.queue.pop(0) if self.queue else b""

        def sendall(self, data):
            self.replied += data

        def close(self):
            pass

    peer = _FakePeer()
    bridge._serve(peer)
    return peer.replied.startswith(b"HTTP/1.1 403") and \
        bridge.requests_served == 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv or "-V" in argv:
        print("%s %s" % (APP_NAME, APP_VERSION))
        return 0
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    if "--self-test" in argv:
        return _self_test()

    for index, arg in enumerate(argv):
        if arg == "--data-dir" and index + 1 < len(argv):
            os.environ["AQUASNOWLLM_HOME"] = argv[index + 1]

    if "--bridge-only" in argv:
        return _bridge_only(argv)

    if QT_API is None:
        sys.stderr.write(
            "%s needs a Qt 4 binding (PyQt4 or PySide) for its window.\n"
            "\n"
            "Python 2.7 does not include Qt, and installing py2app does\n"
            "not install it either. Install the binding separately:\n"
            "\n"
            "    sudo port install py27-pyqt4\n"
            "\n"
            "then check it is visible to this interpreter:\n"
            "\n"
            "    %s -c \"import PyQt4.QtGui; print('Qt4 OK')\"\n"
            "\n"
            "See README.md for the full recipe.\n"
            % (APP_NAME, sys.executable or "python2.7"))
        return 2

    app = QtGui.QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    try:
        app.setApplicationVersion(APP_VERSION)
        app.setOrganizationName(APP_NAME)
        app.setOrganizationDomain("github.com")
    except AttributeError:  # pragma: no cover - very old Qt
        pass
    applied = apply_aqua_style(app)

    window = MainWindow()
    window.show()
    if applied:
        window.statusBar().showMessage("Using the %s style." % applied,
                                       4000)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())

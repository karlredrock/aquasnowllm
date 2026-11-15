#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Offline integration test for AquaSnowLLM's networking layer.

It spins up a throwaway HTTPS server (with its own certificate authority),
starts the bundled HTTP-to-HTTPS bridge against it, and drives the real
``LanguageModel`` client through both transports:

    python2.7 tests/net_test.py        # or python3 tests/net_test.py

Nothing leaves the machine, and no OpenRouter key is needed.  This is the
test that proves the Snow Leopard workaround (a local bridge for a broken
system OpenSSL) actually forwards requests and replies correctly.
"""

from __future__ import unicode_literals, print_function

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import aquasnowllm as A  # noqa: E402  (path juggling above)


def make_certificates(folder):
    """Create a self-signed CA + server certificate with the openssl CLI."""
    key = os.path.join(folder, "ca.key")
    crt = os.path.join(folder, "ca.pem")
    csr = os.path.join(folder, "server.csr")
    ext = os.path.join(folder, "ext.cnf")
    silence = open(os.path.join(folder, "openssl.log"), "wb")

    def run(command, *args):
        code = subprocess.call(command, stdout=silence, stderr=silence)
        if code != 0:
            raise RuntimeError("openssl failed: %s" % " ".join(command))

    with open(ext, "w") as handle:
        handle.write("subjectAltName=DNS:localhost,IP:127.0.0.1\n"
                     "basicConstraints=critical,CA:FALSE\n"
                     "keyUsage=critical,digitalSignature,keyEncipherment\n"
                     "extendedKeyUsage=serverAuth\n")
    # A properly formed CA.  OpenSSL 3.x refuses to treat a trust anchor
    # that has no keyUsage/basicConstraints as a CA at all ("invalid CA
    # certificate"), and Python 3.13+ additionally turns on
    # VERIFY_X509_STRICT by default.  The bundle a user downloads
    # (curl.se/ca/cacert.pem) is well formed, so the fixture must be too --
    # otherwise this test fails for reasons unrelated to AquaSnowLLM.
    ca_cnf = os.path.join(folder, "ca.cnf")
    with open(ca_cnf, "w") as handle:
        handle.write("[req]\ndistinguished_name=dn\n[dn]\n"
                     "[v3_ca]\n"
                     "basicConstraints=critical,CA:TRUE\n"
                     "keyUsage=critical,keyCertSign,cRLSign\n"
                     "subjectKeyIdentifier=hash\n")
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", crt, "-days", "2",
         "-subj", "/CN=AquaSnowLLM Test CA",
         "-config", ca_cnf, "-extensions", "v3_ca"])
    run(["openssl", "req", "-newkey", "rsa:2048", "-nodes",
         "-keyout", os.path.join(folder, "server.key"), "-out", csr,
         "-subj", "/CN=localhost"])
    run(["openssl", "x509", "-req", "-in", csr, "-CA", crt, "-CAkey", key,
         "-CAcreateserial", "-out", os.path.join(folder, "server.pem"),
         "-days", "2", "-extfile", ext])
    silence.close()
    return crt


class FakeOpenRouter(object):
    """A minimal stand-in for https://openrouter.ai/api/v1."""

    def __init__(self, cert, key):
        self.cert = cert
        self.key = key
        self.port = None
        self.requests = []
        self._socket = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.5)
        self._socket = listener
        self.port = listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve)
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(2)

    def _serve(self):
        # a *server* context: make_ssl_context() is intentionally
        # client-only (what the app needs), so build this one by hand
        context = ssl.SSLContext(getattr(ssl, "PROTOCOL_TLS_SERVER",
                                         ssl.PROTOCOL_TLSv1_2))
        context.load_cert_chain(self.cert, self.key)
        while not self._stop.is_set():
            try:
                raw_client, _ = self._socket.accept()
            except socket.timeout:
                continue
            except (socket.error, OSError):
                break
            try:
                raw_client.settimeout(15)
                raw_client.setsockopt(socket.IPPROTO_TCP,
                                      socket.TCP_NODELAY, 1)
                client = context.wrap_socket(raw_client, server_side=True)
            except Exception:  # pragma: no cover - test scaffolding
                # expected while testing an untrusted CA bundle
                try:
                    raw_client.close()
                except Exception:
                    pass
                continue
            try:
                self._handle(client)
            except Exception:  # pragma: no cover - test scaffolding
                pass
            finally:
                try:
                    client.close()
                except Exception:
                    pass

    def _handle(self, client):
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = client.recv(65536)
            if not chunk:
                return
            raw += chunk
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, target = lines[0].split(" ")[:2]
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or 0)
        length = min(length, 1 << 20)
        while len(body) < length:
            chunk = client.recv(65536)
            if not chunk:
                break
            body += chunk
        self.requests.append((method, target, headers, body))
        status, payload = self._respond(method, target, headers, body)
        reply = ("HTTP/1.1 %s\r\nContent-Type: application/json\r\n"
                 "Content-Length: %d\r\nConnection: close\r\n\r\n"
                 % (status, len(payload))).encode("latin-1") + payload
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.sendall(reply)
        try:
            client.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        try:
            while client.recv(65536):
                pass
        except Exception:
            pass

    def _respond(self, method, target, headers, body):
        if target.endswith("/models"):
            return "200 OK", json.dumps({"data": [
                {"id": "openrouter/free", "name": "Free router"},
                {"id": "qwen/qwen3.8-27b:free", "name": "Qwen 3.8"},
                {"id": "openai/gpt-4o", "name": "GPT-4o",
                 "pricing": {"prompt": "0.000005",
                             "completion": "0.000015"}},
            ]}).encode("utf-8")
        if target.endswith("/chat/completions"):
            if headers.get("authorization") != "Bearer test-key":
                return "401 Unauthorized", json.dumps(
                    {"error": {"message": "No auth credentials found"}}
                ).encode("utf-8")
            request = json.loads(body.decode("utf-8"))
            last = request["messages"][-1]["content"]
            answer = {
                "choices": [{"message": {"role": "assistant",
                                         "content": "echo: " + last}}],
                "usage": {"total_tokens": 12},
            }
            return "200 OK", json.dumps(answer).encode("utf-8")
        return "404 Not Found", b'{"error": {"message": "not found"}}'


def _blocked(*args, **kwargs):
    """Force ``LanguageModel`` onto its urllib2 fallback."""
    raise IOError("python ssl transport disabled for this test")


def _describe_environment():
    """Print what the runner has, so a CI failure is diagnosable."""
    print("environment:")
    print("  python      : %s" % sys.version.split()[0])
    print("  platform    : %s" % sys.platform)
    try:
        import ssl
        print("  ssl         : %s" % getattr(ssl, "OPENSSL_VERSION", "?"))
    except Exception as exc:
        print("  ssl         : unavailable (%s)" % exc)
    openssl = None
    for candidate in ("openssl", "/usr/bin/openssl"):
        proc = subprocess.Popen([candidate, "version"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        if proc.returncode == 0:
            openssl = out.decode("utf-8", "replace").strip()
            break
    print("  openssl cli : %s" % (openssl or "NOT FOUND"))
    sys.stdout.flush()


def main():
    failures = []
    _describe_environment()

    def check(label, condition, detail=""):
        if condition:
            print("  ok   %s" % label)
        else:
            failures.append(label)
            print("  FAIL %s   %s" % (label, detail))

    folder = tempfile.mkdtemp(prefix="aquasnow-net-")
    fake = None
    bridge = None
    try:
        try:
            ca = make_certificates(folder)
        except Exception as exc:
            check("generate a test certificate", False, str(exc))
            log = os.path.join(folder, "openssl.log")
            if os.path.exists(log):
                print(open(log, "rb").read().decode("utf-8", "replace")[-800:])
            return 1
        fake = FakeOpenRouter(os.path.join(folder, "server.pem"),
                              os.path.join(folder, "server.key"))
        fake.start()
        # the certificate is issued for localhost (and 127.0.0.1), and
        # host verification is exercised by dialling the DNS name
        upstream = "https://localhost:%d" % fake.port
        print("Fake OpenRouter on %s (CA: %s)" % (upstream, ca))

        # 1. direct TLS with the custom CA bundle
        time.sleep(0.5)
        client = A.LanguageModel("test-key", base_url=upstream,
                                 cacert_path=ca, timeout=20)
        try:
            models = client.list_models()
        except Exception as exc:
            # a broken TLS path must be a named failure, not a traceback:
            # earlier this crashed with a bare "Could not reach <url>" and
            # told us nothing about *why*
            detail = getattr(exc, "detail", "") or "(no detail)"
            check("direct TLS GET /models", False,
                  "%s: %s | %s (transport=%s)"
                  % (type(exc).__name__, exc, detail,
                     getattr(client, "transport", "?")))
            return 1
        check("direct TLS GET /models", len(models) == 3,
              "got %r" % (models,))
        free = [model["id"] for model in models if model["free"]]
        check("free filter", free == ["openrouter/free",
                                     "qwen/qwen3.8-27b:free"],
              "got %r" % (free,))
        check("transport used", client.transport in ("python ssl", "urllib2"),
              client.transport)

        messages = [{"role": "user", "content": "hallo \u00e9"}]
        text, usage = client.chat(messages, model="openrouter/free",
                                  temperature=0.2, max_tokens=64)
        check("direct TLS POST /chat/completions",
              text == u"echo: hallo \u00e9", repr(text))
        check("usage parsed", usage.get("total_tokens") == 12, repr(usage))
        check("headers sent",
              any(header.get("x-title") == "AquaSnowLLM"
                  for _m, _t, header, _b in fake.requests))
        bodies = [json.loads(body.decode("utf-8"))
                  for _m, target, _h, body in fake.requests
                  if target.endswith("/chat/completions") and body]
        check("unicode survives the round trip",
              any(u"hallo \u00e9" == message["content"]
                  for payload in bodies
                  for message in payload.get("messages", [])),
              repr(bodies))

        # 2. the same calls through the bundled bridge (the Snow Leopard
        #    path: cleartext to localhost, TLS performed by the bridge)
        # ``localhost`` resolves to 127.0.0.1, which keeps the bridge's
        # open-relay guard happy while still hitting the fake server
        bridge = A.HTTPBridge("127.0.0.1", 0, ca, upstream=upstream)
        bridge.start()
        time.sleep(0.4)
        check("bridge started", bridge.is_running(), bridge.last_error)
        bridged = A.LanguageModel(
            "test-key", base_url="http://127.0.0.1:%d" % bridge.bound_port,
            cacert_path="",
            bridge=A.SimpleHTTPClient(), timeout=20)
        # Route through the bridge the way the GUI does (BridgeClient
        # needs Qt, so mirror its behaviour: relative path, no host).
        target = "http://127.0.0.1:%d/api/v1/chat/completions" % \
            bridge.bound_port
        response = A.SimpleHTTPClient(timeout=20).request(
            "POST", target,
            A.utf8_json({"model": "openrouter/free",
                         "messages": [{"role": "user", "content": "bridge"}]}),
            bridged.headers())
        check("bridge forwards POST", response.status == 200,
              "%s %r" % (response.status, response.content[:120]))
        payload = json.loads(response.content.decode("utf-8"))
        check("bridge returns the model output",
              payload["choices"][0]["message"]["content"] == "echo: bridge",
              repr(payload)[:200])
        check("bridge counted the request", bridge.requests_served >= 1,
              str(bridge.requests_served))

        # 3. error handling: a bad key must surface as HTTP 401
        bad = A.LanguageModel("wrong", base_url=upstream, cacert_path=ca,
                              timeout=20)
        try:
            bad.chat([{"role": "user", "content": "x"}],
                     model="openrouter/free")
            check("401 raised", False)
        except A.APIError as exc:
            check("401 raised", exc.status == 401, str(exc.status))
            check("401 explained", "API key" in exc.friendly(), exc.friendly())
            check("401 raw detail kept", "auth" in (exc.detail or ""),
                  repr(exc.detail))

        # 4. the urllib2 transport must also honour the custom CA bundle
        #    (it is the third fallback, and the one MacPorts Python 2.7
        #    uses when ssl.SSLContext is awkward)
        urllib_client = A.LanguageModel("test-key", base_url=upstream,
                                        cacert_path=ca, timeout=20)
        urllib_client._request_ssl = _blocked
        models = urllib_client.list_models()
        check("urllib2 fallback works with a custom CA",
              urllib_client.transport == "urllib2" and len(models) == 3,
              "%s / %r" % (urllib_client.transport, models))

        # 5. a missing CA bundle must not crash, only warn
        warned = A.LanguageModel("k", base_url=upstream,
                                 cacert_path=os.path.join(folder, "nope.pem"),
                                 timeout=20)
        try:
            warned.list_models()
        except A.APIError as exc:
            detail = (exc.detail or "").lower()
            check("unknown CA is a clean error",
                  exc.status == 0 and "certificate" in detail,
                  repr(exc.detail)[:160])
        check("missing bundle warned",
              any("was not found" in warning for warning in warned.warnings),
              repr(warned.warnings))
    finally:
        if bridge is not None:
            bridge.stop()
        if fake is not None:
            fake.stop()
        shutil.rmtree(folder, ignore_errors=True)

    print("")
    if failures:
        print("%d check(s) failed." % len(failures))
        return 1
    print("All network checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

AquaSnowLLM bundled resources
=============================

Files in this folder are copied into
``AquaSnowLLM.app/Contents/Resources/`` by ``setup.py`` and are found at
run time by ``aquasnowllm.find_bundle_file()``.

* ``cacert.pem`` (optional) -- a current CA bundle, e.g. from
  https://curl.se/ca/cacert.pem .  When present it is offered as the default
  for Settings -> CA Certificate Bundle, which fixes "certificate verify
  failed" on OS X 10.6 (its system roots expired in 2013).
  Download it with:

      curl -o assets/cacert.pem https://curl.se/ca/cacert.pem

* ``AquaSnowLLM.icns`` (optional) -- the application icon.  If present,
  ``setup.py`` passes it to py2app as ``iconfile``.

Neither file is required to run AquaSnowLLM from source.

* ``AquaSnowLLM-1.1.0.app.zip`` -- a ready-built, double-clickable
  ``AquaSnowLLM.app`` (48 KB), committed here so it has a stable download
  URL that does not depend on the Releases page:

      https://github.com/karlredrock/aquasnowllm/raw/main/assets/AquaSnowLLM-1.1.0.app.zip

  It bundles the tkinter front end (``aquasnow.py``), so it needs no Qt:

  * Mac OS X 10.7 Lion and newer -- unzip, then double-click it.
  * Mac OS X 10.6 Snow Leopard -- install Python 2.7 first, because the
    system Python there is 2.6.1:

          sudo port install python27

  It is *not* frozen: it runs the Mac's own Python, which is why it is so
  small.  Rebuild it with ``python3 tools/build_app.py --zip``.

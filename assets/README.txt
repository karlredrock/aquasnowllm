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

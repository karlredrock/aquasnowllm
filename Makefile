# AquaSnowLLM -- development helpers.
#
# The app targets Python 2.7 + Qt 4.  These targets cover the checks that
# can run on a modern machine; run them all with `make check`.

PY2 ?= python2.7
PY3 ?= python3
PIP  = $(PY3) -m pip

.PHONY: help check syntax selftest net gui app lint run bridge clean

help:
	@echo "make check     run every offline check"
	@echo "make syntax    Python 2.7 syntax gate"
	@echo "make selftest  offline logic tests (both interpreters)"
	@echo "make net       TLS + bridge integration test (needs openssl)"
	@echo "make gui       headless QMainWindow test (needs PyQt5)"
	@echo "make app       build dist/AquaSnowLLM.app (runs anywhere)"
	@echo "make apptest   verify the built bundle"
	@echo "make dmg       .app + zip, ready to hand out"
	@echo "make lint      pyflakes, if installed"
	@echo "make run       launch the app with $(PY2)"
	@echo "make bridge    run the HTTP-to-HTTPS bridge only"

check: syntax selftest net gui apptest

syntax:
	$(PY3) tools/py2_syntax_check.py

selftest:
	-$(PY2) aquasnowllm.py --self-test
	$(PY3) aquasnowllm.py --self-test

net:
	-$(PY2) tests/net_test.py

gui:
	QT_QPA_PLATFORM=offscreen $(PY3) tools/gui_smoke.py

app:
	$(PY3) tools/build_app.py

apptest:
	$(PY3) tools/app_test.py

# The .app plus a zip of it.  For a fully self-contained (frozen) bundle
# run `python2.7 setup.py py2app` on a Mac instead.
dmg: app
	$(PY3) tools/build_app.py --zip

lint:
	-$(PY3) -m pyflakes aquasnowllm.py tests/net_test.py setup.py \
		tools/build_app.py tools/app_test.py

run:
	$(PY2) aquasnowllm.py

bridge:
	$(PY2) aquasnowllm.py --bridge-only --port 8787

clean:
	rm -rf build dist *.egg-info
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} +

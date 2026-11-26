# Build and verify K2 Plus upstream Klipper v0.14.0 firmware, host, config, and OTA .img

K2_BUILD_ROOT ?= /Volumes/Media4tb/k2-mainline-builds
export K2_BUILD_ROOT

PYTHON ?= python3 -B

.PHONY: all fetch mcu native services host ota test test-only publish-release publish-release-dry clean-work

all: test

fetch:
	$(PYTHON) scripts/fetch_sources.py --extract

mcu: fetch
	$(PYTHON) scripts/build_mcu.py

native: fetch
	$(PYTHON) scripts/build_native.py

services: fetch
	$(PYTHON) scripts/build_services.py

host: fetch mcu native services
	$(PYTHON) scripts/assemble_host.py
	$(PYTHON) scripts/verify_host_arm.py

ota: fetch mcu native services host
	$(PYTHON) scripts/build_ota.py

test: ota
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

test-only:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

publish-release-dry:
	$(PYTHON) scripts/publish_github_release.py --dry-run

publish-release:
	$(PYTHON) scripts/publish_github_release.py

clean-work:
	rm -rf "$(K2_BUILD_ROOT)/work/mcu-stage" "$(K2_BUILD_ROOT)/work/native-stage" "$(K2_BUILD_ROOT)/work/ota-stage"

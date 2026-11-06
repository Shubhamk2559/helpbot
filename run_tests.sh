#!/bin/sh
# Python (stdlib unittest, in-memory Mongo fake) + JS (node:test, mocked SDKs). No network, no pip packages needed.
set -e
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -t . -v 2>&1 | grep -v '^{"ev"'
node --test tests/adapters.test.js

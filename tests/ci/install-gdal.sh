#!/usr/bin/env bash
# GDAL's command-line tools on a GitHub runner. The runner image fetches every package from
# security.ubuntu.com, which runners couldn't reach on 11 Nov 2026; Azure's Ubuntu mirror, next to
# the runners, has the same packages. Downloads are retried.
set -euo pipefail
sudo find /etc/apt -name '*.sources' -o -name '*.list' | xargs -r sudo sed -i \
  -e 's#https\?://security.ubuntu.com/ubuntu#http://azure.archive.ubuntu.com/ubuntu#g' \
  -e 's#https\?://archive.ubuntu.com/ubuntu#http://azure.archive.ubuntu.com/ubuntu#g'
sudo apt-get -o Acquire::Retries=5 update
sudo apt-get -o Acquire::Retries=5 install -y gdal-bin

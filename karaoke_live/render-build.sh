#!/usr/bin/env bash
set -eux
apt-get update
apt-get install -y ffmpeg
pip install -r requirements-app2.txt

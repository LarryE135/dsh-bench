#!/usr/bin/env bash
# DSH-Bench macOS/Linux 入口，例如：./run.sh doctor
cd "$(dirname "$0")" && exec python3 bench/bench.py "$@"

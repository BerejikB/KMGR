#!/usr/bin/env bash
set -eu
DIR=/mnt/k/GOOSE/KMGR/scratch
iconv -f UTF-16LE -t UTF-8 "$DIR/manual_stderr.txt" > "$DIR/manual_stderr.utf8.txt" || true
iconv -f UTF-16LE -t UTF-8 "$DIR/manual_all.txt" > "$DIR/manual_all.utf8.txt" || true
printf 'sizes:\n'
wc -c "$DIR"/manual_*.utf8.txt || true

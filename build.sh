#!/bin/sh
# thought-net 再生成（1コマンド）: sh ~/Projects/thought-net/build.sh
exec python3 "$(dirname "$0")/build.py"

#!/bin/bash
# Запуск USB-моста на Mac. Если на компьютере нет Python и инструментов разработчика, ничего не скачивается:
# используется bash-версия (bridge.sh).
cd "$(dirname "$0")"
if xcode-select -p >/dev/null 2>&1 && python3 --version >/dev/null 2>&1; then
  [ -d venv ] || { python3 -m venv venv && ./venv/bin/pip install --quiet pyserial; }
  ./venv/bin/python usb_bridge.py "$@"
else
  bash ./bridge.sh "$@"
fi

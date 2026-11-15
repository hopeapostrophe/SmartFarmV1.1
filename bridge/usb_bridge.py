#!/usr/bin/env python3
"""USB-мост: читает Arduino/ESP32 по USB (кабель Type-C) и отправляет строки на сервер.

Работает на Windows, macOS и Linux.

Windows (рекомендуется):  двойной клик по bridge\\start_windows.bat
                          или: powershell -ExecutionPolicy Bypass -File bridge\\start_windows.ps1
macOS:                    bash bridge/start_mac.sh
Linux (без Docker):       python3 bridge/usb_bridge.py

Показать порты:   ... start_windows.ps1 --list   (или python usb_bridge.py --list)
Проверить сервер: ... start_windows.ps1 --send-test

Скорость и порт определяются сами. Arduino перезагружается при открытии порта —
это нормально, поэтому первые секунды данные могут не идти.
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("Не найден pyserial. Установите: python -m pip install pyserial\n"
             "Либо запустите bridge\\start_windows.ps1 — он всё поставит сам.")

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
BAUDS = (9600, 115200, 57600, 38400, 19200)

# USB-переходники, которые стоят на Arduino/ESP32-платах и клонах
USB_VIDS = {0x2341, 0x2A03, 0x239A, 0x303A, 0x1A86, 0x10C4, 0x0403, 0x067B, 0x0483, 0x16C0}
USB_HINT = re.compile(r"arduino|ch340|ch341|cp210|ft232|ftdi|usb[-\s]?serial|silicon labs|wch|esp32|esp8266|mega|uno|nano", re.I)
MAC_PATTERNS = ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*", "/dev/cu.SLAB_USBtoUART*",
                "/dev/tty.usbmodem*", "/dev/tty.usbserial*")
LINUX_PATTERNS = ("/dev/ttyACM*", "/dev/ttyUSB*")


def _describe(p):
    parts = [getattr(p, "description", "") or "", getattr(p, "manufacturer", "") or "",
             getattr(p, "product", "") or "", getattr(p, "hwid", "") or ""]
    return " ".join(x for x in parts if x).strip()


def _all_ports():
    """Все последовательные порты системы с описанием и признаком «похож на Arduino»."""
    ports = []
    for p in list_ports.comports():
        text = _describe(p)
        known = (getattr(p, "vid", None) in USB_VIDS
                 or bool(USB_HINT.search(text))
                 or "USB" in (getattr(p, "hwid", "") or "").upper())
        ports.append({"port": p.device, "description": text or p.device, "known": known})
    return ports


def _port_key(item):
    digits = re.sub(r"\D", "", item["port"])
    return (not item["known"], int(digits) if IS_WIN and digits else 0, item["port"])


def candidates():
    """Порты-кандидаты: сначала явные USB (Arduino, CH340, CP210x…), потом остальные."""
    ports = _all_ports()
    if IS_WIN:
        return sorted(ports, key=_port_key)          # COM-порты всегда перечисляются системой
    found = {p["port"] for p in ports}
    patterns = MAC_PATTERNS if IS_MAC else LINUX_PATTERNS
    for pat in patterns:
        for dev in glob.glob(pat):
            if dev not in found:
                found.add(dev)
                ports.append({"port": dev, "description": dev, "known": True})
    return sorted(ports, key=_port_key)


def normalize_port(value):
    v = (value or "").strip()
    if IS_WIN and re.fullmatch(r"\d+", v):           # --port 5 -> COM5
        return "COM" + v
    return v


def looks_like_text(line):
    return bool(line) and "\ufffd" not in line and line.isprintable() and any(c.isdigit() for c in line)


def detect_baud(port):
    """Перебирает частые скорости и берёт ту, при которой приходит читаемый текст."""
    for b in BAUDS:
        try:
            with serial.Serial(port, b, timeout=1) as s:
                time.sleep(2)                        # Arduino перезагружается при открытии порта
                s.reset_input_buffer()
                end, good, total = time.time() + 4, 0, 0
                while time.time() < end:
                    raw = s.readline()
                    if not raw:
                        continue
                    total += 1
                    good += looks_like_text(raw.decode("utf-8", errors="replace").strip())
                if good >= 2 and good >= total * 0.8:
                    return b
        except (serial.SerialException, OSError, ValueError):
            pass
    return None


def post(url, line, key):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-Sensor-Key"] = key
    body = json.dumps({"line": line}, ensure_ascii=False).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/v1/sensor/ingest", data=body, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return None
    except urllib.error.HTTPError as e:
        if e.code == 422:
            return "сервер не нашёл влажность в строке (нужно, например, «moisture: 45»)"
        if e.code == 401:
            return "неверный ключ (--key)"
        return f"сервер ответил {e.code}: {e.read().decode(errors='ignore')[:150]}"
    except Exception as e:
        return f"сервер недоступен ({e}). Запущен ли Docker и сервер на {url} ?"


def print_ports():
    ports = candidates()
    if not ports:
        print("USB-устройства не найдены. Подключите Arduino кабелем Type-C (кабель должен быть для данных, не только для зарядки).")
        return
    print("Найденные порты:")
    for p in ports:
        mark = "  <- похож на Arduino" if p["known"] else ""
        print(f"  {p['port']:<10} {p['description']}{mark}")
    print("\nЗапуск с выбором порта: --port COM5   (или просто --port 5)")


def main():
    ap = argparse.ArgumentParser(description="USB-мост Arduino -> Smart Farming AI")
    ap.add_argument("--port", default="", help="COM5 / /dev/cu.usbmodemXXXX (пусто = определить сам)")
    ap.add_argument("--baud", type=int, default=0, help="0 = определить сам")
    ap.add_argument("--url", default=os.getenv("AGRO_SERVER_URL", "http://localhost:8001"),
                    help="адрес сервера (по умолчанию http://localhost:8001 — копия «agro copy — session 2»)")
    ap.add_argument("--key", default=os.getenv("SENSOR_KEY", ""), help="ключ датчика, если задан SENSOR_KEY")
    ap.add_argument("--list", action="store_true", help="показать найденные USB-порты и выйти")
    ap.add_argument("--send-test", action="store_true", help="отправить тестовое показание и выйти")
    a = ap.parse_args()

    a.port = normalize_port(a.port)

    if a.list:
        print_ports()
        return
    if a.send_test:
        err = post(a.url, "moisture: 42", a.key)
        print("OK: сервер принял тестовое показание moisture: 42" if not err else "Ошибка: " + err)
        sys.exit(1 if err else 0)

    print("Мост запущен. Остановить: Ctrl+C")
    last_sent = 0.0
    while True:
        port = a.port
        if not port:
            found = candidates()
            port = found[0]["port"] if found else None
        if not port:
            print("USB-устройство не найдено. Подключите Arduino кабелем Type-C для данных "
                  "и закройте монитор порта в Arduino IDE...")
            time.sleep(3)
            continue
        try:
            baud = a.baud
            if not baud:
                print(f"Найден порт {port}. Определяю скорость (около 20 секунд)...")
                baud = detect_baud(port)
                if not baud:
                    print("Скорость определить не удалось, пробую 9600. Если данных нет, укажите --baud 115200")
                    baud = 9600
            with serial.Serial(port, baud, timeout=2) as s:
                print(f"Подключено: {port}, скорость {baud}")
                time.sleep(2)                        # Arduino перезагружается при открытии порта
                last_line, hinted = time.time(), False
                while True:
                    line = s.readline().decode("utf-8", errors="replace").strip()
                    if not line:
                        if not hinted and time.time() - last_line > 10:
                            print("Данных нет 10 секунд. Проверьте, что скетч печатает через Serial.println "
                                  "и что скорость верная (--baud 115200).")
                            hinted = True
                        continue
                    last_line, hinted = time.time(), False
                    print("<-", line)
                    if time.time() - last_sent >= 1:  # не чаще раза в секунду
                        err = post(a.url, line, a.key)
                        last_sent = time.time()
                        print("  отправлено" if not err else f"  ошибка: {err}")
        except serial.SerialException as e:
            msg = str(e)
            if IS_WIN and ("access is denied" in msg.lower() or "отказано" in msg.lower()):
                print(f"Порт {port} занят другой программой. Закройте монитор порта в Arduino IDE, "
                      "Serial Monitor, PuTTY и повторите.")
            else:
                print(f"Порт потерян или не открылся ({msg}). Повтор через 3 с...")
            time.sleep(3)
        except KeyboardInterrupt:
            print("\nОстановлено.")
            return


if __name__ == "__main__":
    main()

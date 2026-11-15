#!/bin/bash
# USB-мост без установки программ: используются только встроенные в macOS bash, stty и curl.
# Запуск: bash bridge/bridge.sh [--baud 9600] [--port /dev/cu.usbmodemXXX] [--url http://localhost:8000] [--key КЛЮЧ] [--list]
export LC_ALL=C
URL="http://localhost:8001"; KEY=""; BAUD=""; PORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --baud) BAUD="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --url)  URL="$2";  shift 2;;
    --key)  KEY="$2";  shift 2;;
    --list) ls /dev/cu.usb* /dev/cu.wchusb* /dev/cu.SLAB* /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "USB-устройства не найдены"; exit 0;;
    *) shift;;
  esac
done
case "$(uname)" in Darwin) SF=-f;; *) SF=-F;; esac

find_port() { ls /dev/cu.usbmodem* /dev/cu.usbserial* /dev/cu.wchusbserial* /dev/cu.SLAB_USBtoUART* /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -1; }
set_speed() { [ -n "$NO_STTY" ] || stty $SF "$PORT" "$1" raw -echo cs8 -parenb -cstopb clocal -hupcl 2>/dev/null; }
good_line() { [[ "$1" =~ ^[[:print:][:space:]]*[0-9][[:print:][:space:]]*$ ]]; }

detect_baud() {
  local b line good total end
  for b in 9600 115200 57600 38400 19200; do
    exec 3<>"$PORT" 2>/dev/null || continue
    set_speed "$b"; sleep 2
    good=0; total=0; end=$((SECONDS+4))
    while [ $SECONDS -lt $end ]; do
      if IFS= read -r -t 1 line <&3; then
        total=$((total+1)); line=${line%$'\r'}
        good_line "$line" && good=$((good+1))
      fi
    done
    exec 3<&-
    if [ $good -ge 2 ] && [ $((good*10)) -ge $((total*8)) ]; then echo "$b"; return; fi
  done
}

send() {
  local esc code
  esc=$(printf '%s' "$1" | tr -d '\000-\037')
  esc=${esc//\\/\\\\}; esc=${esc//\"/\\\"}
  if [ -n "$KEY" ]; then
    code=$(curl -s -m 8 -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" -H "X-Sensor-Key: $KEY" -d "{\"line\":\"$esc\"}" "$URL/api/v1/sensor/ingest" 2>/dev/null)
  else
    code=$(curl -s -m 8 -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" -d "{\"line\":\"$esc\"}" "$URL/api/v1/sensor/ingest" 2>/dev/null)
  fi
  case "$code" in
    200) echo "  отправлено";;
    422) echo "  сервер не нашёл влажность в этой строке (нужно что-то вроде moisture: 45)";;
    401) echo "  неверный ключ (--key)";;
    000) echo "  сервер недоступен: запущен ли Docker и сервер на $URL ?";;
    *)   echo "  ответ сервера: $code";;
  esac
}

echo "Мост запущен. Остановить: Ctrl+C"
while true; do
  P="$PORT"; [ -z "$P" ] && P=$(find_port)
  if [ -z "$P" ]; then echo "USB-устройство не найдено. Подключите датчик кабелем для передачи данных и закройте монитор порта в Arduino IDE..."; sleep 3; continue; fi
  PORT="$P"
  B="$BAUD"
  if [ -z "$B" ]; then
    echo "Найден порт $PORT. Определяю скорость (до 30 секунд)..."
    B=$(detect_baud)
    [ -z "$B" ] && { echo "Скорость определить не удалось, пробую 9600. Если данных нет, запустите с --baud 115200"; B=9600; }
  fi
  exec 3<>"$PORT" || { echo "Не удалось открыть $PORT. Возможно, порт занят другой программой."; sleep 3; continue; }
  set_speed "$B"; echo "Подключено: $PORT, скорость $B"; sleep 2
  last=0; lastline=$SECONDS; hinted=0
  while true; do
    if IFS= read -r -t 2 line <&3; then
      line=${line%$'\r'}; [ -z "$line" ] && continue
      lastline=$SECONDS; hinted=0
      echo "← $line"
      if [ "$SECONDS" -ne "$last" ]; then send "$line"; last=$SECONDS; fi
    else
      [ ! -e "$PORT" ] && { echo "Порт потерян. Повтор через 3 с..."; break; }
      if [ $hinted -eq 0 ] && [ $((SECONDS-lastline)) -gt 10 ]; then
        echo "Данных нет 10 секунд. Проверьте, что скетч печатает через Serial.println и что скорость верная (--baud 115200)."; hinted=1
      fi
    fi
  done
  exec 3<&-; sleep 3
done

# USB-мост для Windows без установки Python.
# Читает Arduino/ESP32 с COM-порта (кабель Type-C) и отправляет строки на сервер.
# Запуск: powershell -ExecutionPolicy Bypass -File bridge\bridge_windows.ps1
#         ... --list        показать COM-порты
#         ... --send-test   проверить связь с сервером
#         ... --port COM5 --baud 9600 --url http://localhost:8001 --key КЛЮЧ

# ---- Разбор аргументов (в стиле usb_bridge.py) ----
$Port = ""
$Baud = 0
$Url = "http://localhost:8001"   # сервер «agro copy — session 2» (docker compose) на хост-порту 8001
$Key = ""
$List = $false
$SendTest = $false

for ($i = 0; $i -lt $args.Count; $i++) {
    switch -Regex ([string]$args[$i]) {
        '^--port$'      { $i++; $Port = [string]$args[$i]; break }
        '^--baud$'      { $i++; $Baud = [int]$args[$i]; break }
        '^--url$'       { $i++; $Url = [string]$args[$i]; break }
        '^--key$'       { $i++; $Key = [string]$args[$i]; break }
        '^--list$'      { $List = $true; break }
        '^--send-test$' { $SendTest = $true; break }
        default         { Write-Host "Неизвестный параметр: $($args[$i])" }
    }
}
if ($Port -match '^\d+$') { $Port = "COM$Port" }

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Get-UsbPorts {
    $desc = @{}
    try {
        Get-CimInstance Win32_PnPEntity -ErrorAction Stop |
            Where-Object { $_.Name -match '\(COM\d+\)' } |
            ForEach-Object {
                $m = [regex]::Match($_.Name, '\((COM\d+)\)')
                if ($m.Success) { $desc[$m.Groups[1].Value] = $_.Name }
            }
    } catch { }

    $list = @()
    foreach ($name in [System.IO.Ports.SerialPort]::GetPortNames()) {
        $d = ""
        if ($desc.ContainsKey($name)) { $d = $desc[$name] }
        $known = $d -match 'Arduino|CH340|CH341|CP210|FT232|FTDI|USB[- ]?Serial|Silicon Labs|WCH|ESP32|ESP8266|Mega|Uno|Nano'
        $list += [pscustomobject]@{ Port = $name; Description = $d; Known = [bool]$known }
    }
    $list | Sort-Object -Property @{ Expression = { $_.Known }; Descending = $true }, @{ Expression = { [int]($_.Port -replace '\D', '') } }
}

function Send-Line([string]$text) {
    $body = @{ line = $text } | ConvertTo-Json -Compress
    $headers = @{ "Content-Type" = "application/json; charset=utf-8" }
    if ($Key) { $headers["X-Sensor-Key"] = $Key }
    try {
        Invoke-RestMethod -Method Post -Uri ($Url.TrimEnd('/') + "/api/v1/sensor/ingest") `
            -Headers $headers -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 10 | Out-Null
        return $null
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
        switch ($code) {
            422 { return "сервер не нашёл влажность в строке (нужно, например, «moisture: 45»)" }
            401 { return "неверный ключ (--key)" }
            0   { return "сервер недоступен. Запущен ли Docker и сервер на $Url ?" }
            default { return "сервер ответил $code" }
        }
    }
}

function Test-Baud([string]$p, [int]$b) {
    $sp = New-Object System.IO.Ports.SerialPort $p, $b, ([System.IO.Ports.Parity]::None), 8, ([System.IO.Ports.StopBits]::One)
    $sp.ReadTimeout = 1000
    $sp.Encoding = [System.Text.Encoding]::UTF8
    try {
        $sp.Open()
        Start-Sleep -Seconds 2
        $sp.DiscardInBuffer()
        $end = (Get-Date).AddSeconds(4)
        $good = 0
        $total = 0
        while ((Get-Date) -lt $end) {
            try { $line = $sp.ReadLine().Trim() } catch { continue }
            $total++
            if ($line -match '\d' -and $line -notmatch [char]0xFFFD) { $good++ }
        }
        return ($good -ge 2 -and ($good * 10) -ge ($total * 8))
    } catch {
        return $false
    } finally {
        try { if ($sp.IsOpen) { $sp.Close() } } catch { }
        $sp.Dispose()
    }
}

function Write-Ports {
    $ports = Get-UsbPorts
    if (-not $ports) {
        Write-Host "USB-устройства не найдены. Подключите Arduino кабелем Type-C (кабель должен быть для данных, не только для зарядки)."
        return
    }
    Write-Host "Найденные порты:"
    foreach ($p in $ports) {
        $mark = ""
        if ($p.Known) { $mark = "  <- похож на Arduino" }
        Write-Host ("  {0,-10} {1}{2}" -f $p.Port, $p.Description, $mark)
    }
    Write-Host ""
    Write-Host "Запуск с выбором порта: --port COM5   (или просто --port 5)"
}

if ($List) { Write-Ports; exit 0 }

if ($SendTest) {
    $err = Send-Line "moisture: 42"
    if ($err) { Write-Host "Ошибка: $err"; exit 1 }
    Write-Host "OK: сервер принял тестовое показание moisture: 42"
    exit 0
}

Write-Host "Мост запущен. Остановить: Ctrl+C"
$lastSent = [datetime]::MinValue

while ($true) {
    $p = $Port
    if (-not $p) {
        $found = Get-UsbPorts | Where-Object { $_.Known } | Select-Object -First 1
        if (-not $found) {
            Write-Host "USB-устройство не найдено. Подключите Arduino кабелем Type-C для данных и закройте монитор порта в Arduino IDE..."
            Start-Sleep -Seconds 3
            continue
        }
        $p = $found.Port
    }

    $b = $Baud
    if (-not $b) {
        Write-Host "Найден порт $p. Определяю скорость (около 20 секунд)..."
        foreach ($tryBaud in 9600, 115200, 57600, 38400, 19200) {
            if (Test-Baud $p $tryBaud) { $b = $tryBaud; break }
        }
        if (-not $b) {
            Write-Host "Скорость определить не удалось, пробую 9600. Если данных нет, укажите --baud 115200"
            $b = 9600
        }
    }

    $sp = New-Object System.IO.Ports.SerialPort $p, $b, ([System.IO.Ports.Parity]::None), 8, ([System.IO.Ports.StopBits]::One)
    $sp.ReadTimeout = 2000
    $sp.DtrEnable = $true
    $sp.Encoding = [System.Text.Encoding]::UTF8

    try {
        $sp.Open()
        Write-Host "Подключено: $p, скорость $b"
        Start-Sleep -Seconds 2
        $lastLineAt = Get-Date
        $hinted = $false

        while ($true) {
            try {
                $line = $sp.ReadLine().Trim()
            } catch [System.TimeoutException] {
                if (-not $hinted -and ((Get-Date) - $lastLineAt).TotalSeconds -gt 10) {
                    Write-Host "Данных нет 10 секунд. Проверьте, что скетч печатает через Serial.println и что скорость верная (--baud 115200)."
                    $hinted = $true
                }
                continue
            }

            if (-not $line) { continue }
            $lastLineAt = Get-Date
            $hinted = $false
            Write-Host "<- $line"

            if (((Get-Date) - $lastSent).TotalSeconds -ge 1) {
                $err = Send-Line $line
                $lastSent = Get-Date
                if ($err) { Write-Host "  ошибка: $err" } else { Write-Host "  отправлено" }
            }
        }
    } catch {
        Write-Host "Порт $p потерян или занят: $($_.Exception.Message)"
        Write-Host "Если порт занят — закройте монитор порта Arduino IDE, PuTTY и другие программы. Повтор через 3 с..."
        Start-Sleep -Seconds 3
    } finally {
        try { if ($sp.IsOpen) { $sp.Close() } } catch { }
        $sp.Dispose()
    }
}

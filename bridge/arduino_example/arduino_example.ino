/*
  Пример скетча для USB-моста Smart Farming AI (Arduino Uno / Nano / ESP32).

  Аналоговый датчик влажности почвы подключается к A0 (питание 5V/3.3V и GND).
  Мост сам открывает COM-порт, поэтому монитор порта в Arduino IDE должен быть закрыт.

  Формат строк, который понимает сервер:
    moisture: 512   — сырое значение АЦП (сервер пересчитает по калибровке)
    moisture: 45    — проценты
    Влажность: 45   — тоже можно
*/

const int sensorPin = A0;

void setup() {
  Serial.begin(9600);   // мост определяет скорость сам; 9600 — надёжный вариант
  delay(1500);
}

void loop() {
  int raw = analogRead(sensorPin);
  Serial.print("moisture: ");
  Serial.println(raw);
  delay(2000);          // одно показание в 2 секунды
}

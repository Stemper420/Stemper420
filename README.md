# MADRIX 5 -> Hue Entertainment bridge

Этот проект принимает Art-Net от MADRIX 5 и транслирует его в Philips Hue Entertainment Area.

## Схема

```text
MADRIX 5 -> Art-Net Unicast/Loopback -> madrix_hue_bridge.py -> Hue Bridge -> Entertainment Area
```

## Что умеет

- `pair` — получить `username` и `clientkey` с Hue Bridge
- `areas` — вывести доступные Entertainment Area и `channel_id`
- `run` — слушать Art-Net и отправлять RGB в Hue

## Установка

```bash
py -3.10 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Важно: `hue-entertainment-pykit` требует Python `3.10–3.12`. На `Python 3.13` зависимости не установятся.

## 1. Pairing

Нажми физическую кнопку на Hue Bridge и выполни:

```bash
python madrix_hue_bridge.py pair --bridge-ip 192.168.1.100 --write-config bridge.json
```

Скрипт напечатает:
- `username`
- `clientkey`
- список Entertainment Areas
- starter config

## 2. Выбор зоны

Посмотреть зоны ещё раз можно так:

```bash
python madrix_hue_bridge.py areas \
  --bridge-ip 192.168.1.100 \
  --username YOUR_USERNAME \
  --clientkey YOUR_CLIENTKEY
```

Укажи нужный `entertainment_area_id` в JSON-конфиге.

## 3. Запуск моста

```bash
python madrix_hue_bridge.py run --config config.example.json
```

## GUI-монитор

```bash
python madrix_hue_bridge_gui.py --config config.example.json
```

GUI позволяет:
- загрузить и сохранить конфиг
- сделать `Pair`
- посмотреть список Entertainment Areas
- запустить мост
- визуально видеть активность Art-Net и текущие RGB по каналам

## Сборка EXE

```powershell
./build_exe.ps1
```

Скрипт собирает `dist/MadrixHueBridgeUI.exe`.

## Установочный файл

```powershell
./build_installer.ps1
```

Скрипт собирает `dist/MadrixHueBridgeSetup.exe`.
Установщик:
- копирует приложение в `%LocalAppData%\Programs\MadrixHueBridge`
- кладет рядом `config.example.json`, `config.json`, `README.md`
- создает ярлык на рабочем столе и в меню Пуск

## Настройка MADRIX 5

Вариант 1: loopback / localhost
- Art-Net device -> Unicast
- IP назначения: `127.0.0.1`
- порт Art-Net стандартный: `6454`
- Universe / Port Address должен совпадать с `mapping.port_address_start`

Вариант 2: локальный IP компьютера
- если MADRIX и мост работают на той же машине, можно использовать IP интерфейса вместо loopback

## Mapping

### Sequential

Каждый Hue channel получает 3 DMX-канала подряд:
- channel 0 -> DMX 1/2/3
- channel 1 -> DMX 4/5/6
- и так далее

Пример:

```json
"mapping": {
  "type": "sequential_rgb",
  "port_address_start": 0,
  "dmx_start": 1,
  "channel_order": "RGB",
  "channels": [0, 1, 2, 3]
}
```

### Explicit

```json
"mapping": {
  "type": "explicit",
  "items": [
    {
      "channel_id": 0,
      "r": {"port_address": 0, "dmx_address": 1},
      "g": {"port_address": 0, "dmx_address": 2},
      "b": {"port_address": 0, "dmx_address": 3}
    }
  ]
}
```

## Ограничения

- Этот мост опирается на `hue-entertainment-pykit`.
- На момент подготовки нужен Python 3.10–3.12.
- Нужна существующая Entertainment Area, созданная в приложении Hue.
- Скрипт не был протестирован на твоей конкретной сети/Bridge/MADRIX-конфигурации, поэтому возможна доработка по месту.

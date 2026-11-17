# Квартира в Екатеринбурге

Живая конфигурация Home Assistant на HAOS. Репозиторий хранит **логику**, не снимок системы. Восстановление — из бэкапа HAOS, не из git.

Соседний проект: `../home-assistant-house` (дом в Шадринске, отдельный репозиторий). Общие приёмы есть, железо и `entity_id` разные — не копировать автоматизации один в один.

Подробный обзор для людей: `README.md`. Этот файл — рабочая выжимка для правок.

## Границы репозитория

`.gitignore` игнорирует всё, кроме корневых `*.yaml`, `*.py`, `README.md`, `AGENTS.md`.

В git не попадает и не должно попадать: `secrets.yaml`, токены, `chat_id`, IP/MAC, `entity_id` Яндекс.Станции, темы (`themes/`), базы, логи, runtime Zigbee2MQTT. Чувствительное — только через `!secret`.

## Стек

- Zigbee через **Zigbee2MQTT**, префикс топиков `zigbee2mqtt_1`. Кнопки часто слушаются MQTT напрямую (`.../action`), датчики — и через сущности HA.
- Уведомления: `notify.send_message` на `!secret telegram_notify_entity` (групповой чат Telegram). Сообщение мониторинга Zigbee с HTML — `telegram_bot.send_message`. Личный Мештастик — `!secret telegram_personal_notify_entity`, не групповой чат. Голос на Яндекс.Станции (`!secret yandex_station_main`) только для срочного: протечки и режим «уехали».
- Дашборд в YAML-режиме: `ui-lovelace.yaml`, заголовок «Квартира в Екб». Вкладки: Сервер HA, Квартира, Погода, Камеры, Климат.
- Люди: `device_tracker.alex`, `device_tracker.tanya` в `group.family`. Присутствие в автоматизациях завязано на **alex**, не на группу.

## Файлы

| Файл | Роль |
|---|---|
| `configuration.yaml` | helpers, `schedule.warm_floor`, template-сенсоры, MQTT-освещённость, `command_line` сенсор Zigbee, Telegram, Lovelace |
| `automations.yaml` | вся логика. Синтаксис смешан: старый `trigger`/`action`/`service` и новый `triggers`/`actions` |
| `ui-lovelace.yaml` | дашборд |
| `check_zigbee.py` | диагностика Z2M, вызывается сенсором каждые 5 мин |
| `scripts.yaml`, `scenes.yaml`, `known_devices.yaml` | пустые заглушки |

## Как устроена логика

**Тёплый пол — деманд + синхронизатор, не цепочка событий.** `binary_sensor.warm_floor_demand` истинен, когда одновременно: `input_boolean.warm_floor_enabled` on, `input_boolean.away_home` off, `schedule.warm_floor` on (08:00–10:00 и 21:00–23:00), фаза цикла `< period × duty / 100`. Цикл: `input_number.warm_floor_period_min`, `input_number.warm_floor_duty_pct`. Автоматизация `warm_floor_sync` приводит `switch.warm_floor_relay` к деманду при смене деманда, возврате реле из `unavailable`, старте HA и каждые 5 минут. Не переписывать на «включить по событию».

**Zigbee-мониторинг.** `check_zigbee.py` читает список устройств Z2M, тип питания и `lastSeen` из `database.db`, плюс `state.json`. Возраст — более свежая из двух меток: на этой установке `last_seen` в `state.json` отстаёт на часы, живая метка лежит в `database.db`. Всегда печатает ровно один JSON и выходит с кодом 0 (`ok` / `offline` / `stale` / `error`). Z2M обязан иметь `advanced.last_seen: ISO_8601`. `state.json` старше 30 минут — статус `stale` (Z2M пишет файл каждые 5 минут). Питание mains (розетки и реле) — порог 2 часа; если молчат все такие устройства сразу, в отчёте это сеть. Датчик с `water_leak` в кэше — 3 часа. Остальные — `DEFAULT_MAX_AGE_H` (24 ч). Сузить точечно — `MAX_AGE_OVERRIDES_H` после `--ages`. Уведомление (`zigbee_monitor_alert`) идёт по смене атрибута `problem_key`, плюс напоминание в 10:00 (в том числе если сенсор `unavailable` дольше часа), плюс разовый алерт если сенсор стал `unavailable`.

**Протечки.** Ванная и кухня: Telegram, голос и перекрытие воды. Батареи (столовая, спальня): Telegram и голос, без крана. Краны разные по действию, это не ошибка: ванная — `switch.turn_off` на `switch.kran_kholodnoi_vody_v_vannoi_switch_1`, кухня — `switch.turn_on` на `switch.kitchen_tap_plug`.

**Уехали надолго.** Двойное нажатие кнопки 4 коридорного выключателя (`sensor.coridor_switch4_action` → `4_double`) включает `input_boolean.away_home`, ставит `climate.bedroom_battery` на 16 °C, выключает `switch.boiler_plug` и кран холодной воды в ванной. Выключение режима — вручную. Голос есть.

**Присутствие.** Уход `device_tracker.alex` включает `input_boolean.notify_door`, приход выключает. Дверь (`binary_sensor.door_sensor_contact`) пишет в Telegram только при `notify_door` on. Контакт: сработка на переход `on` → `off`.

**Свет.** Коридор: кнопки `1_single` / `3_single` на `zigbee2mqtt_1/coridor_switch4/action` (люстра / подсветка двери). Подсветка двери ещё по движению, если `input_boolean.coridor_motion` on и освещённость `< 8` lx. Спальня: `bedroom_switch` — `single_right` кроватный, `single_left` основной+амбилайт, `double_right` ночной. Раковина и вентилятор туалета — отдельные MQTT-кнопки. Люстры спальни и коридора гасятся, если горят дольше 5 часов (проверка каждые 30 мин).

**Ванная.** MQTT `zigbee2mqtt_1/bath_sensor`: влажность > 50 % включает `switch.air_vent_relay` на 5 минут. Кнопка в туалете переключает то же реле.

**Meshtastic.** Личное на `meshtastic.gateway_a1ex_dm` уходит в личный Telegram. Свои исходящие отсекаются по длинному имени шлюза, каналы не пересылаются. Ответ ищется по `input_text.meshtastic_reply_routes` (номер сообщения Telegram и `!id` ноды, последние 10): polling цитату текстом не отдаёт. Лимит отправки — 100 русских букв или 200 латинских. Успех — реакция на ответе.

**Прочее.** Полив: `sensor.plant_sensor_soil_moisture` < 100, плюс напоминание в 10:00. Закат: сообщение «ЕКБ» с температурой балкона и кухни — heartbeat, что HA жив.

## Сервер

Снимок 17 ноября 2026, около 17:15 по местному времени. Вход с компьютера в квартире по SSH: `hassio@homeassistant.local`, ключ не в git. Неинтерактивные команды `ha` нужно оборачивать в `bash -lc`, иначе нет `SUPERVISOR_TOKEN`.

Плата Raspberry Pi 4, ядро `6.18.59-haos-raspi`. HAOS **18.5** со слота B, слот A хранит 18.4, оба хорошие. Диск 28.5 ГБ (занято 11, свободно 16.4). Core **2026.11.1**, к установке **2026.11.2**. Supervisor **2026.11.0**, stable, `healthy`, `supported`. NTP включён, `dt_synchronized` в момент проверки был выключен.

Логика в `/homeassistant` совпала с этой папкой: `configuration.yaml`, `automations.yaml`, `ui-lovelace.yaml`, `check_zigbee.py`, пустые `scripts.yaml` и `scenes.yaml`. `AGENTS.md` на сервер не выкладывается.

Сущностей 2100. Автоматизации из yaml: **26** `on`. Ещё **29** `automation.*` в `unavailable` — старый реестр. `sensor.zigbee_check` был `ok`, 23 устройства. Режим «уехали» был включён, деманд тёплого пола и реле выключены.

## Правила правок

- Не добавлять секреты, адреса и персональные id в yaml. Новый секрет — `!secret`, значение остаётся в `secrets.yaml` на HAOS.
- Не устраивать массовую перепись синтаксиса автоматизаций, пока об этом не попросили. В README это отдельный TODO.
- Повторяющийся паттерн (протечки, кнопки) пока не вынесен в blueprint. Новый похожий кусок можно писать рядом, не рефакторить все сразу.
- Сохранять защиту от ложных срабатываний: `for:`, `expire_after` у MQTT-освещённости (2 ч), проверки `unknown`/`unavailable`.
- Голос не добавлять к обычным уведомлениям.
- Комментарии в yaml — часть документации, не вычищать при мелкой правке.

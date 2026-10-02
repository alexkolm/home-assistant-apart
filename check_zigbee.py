#!/usr/bin/env python3
"""
Мониторинг Zigbee2MQTT по state.json + списку устройств из configuration.yaml.

Контракт:
  * всегда печатает РОВНО ОДИН JSON-объект в stdout и завершается с кодом 0,
    даже при внутренних ошибках (тогда status="error") — так command_line-сенсор
    HA никогда не остаётся с устаревшими данными;
  * никаких промежуточных файлов: единственный источник правды — сенсор.

Статусы: ok | offline | stale | error

Режимы:
  python3 check_zigbee.py         # JSON для сенсора
  python3 check_zigbee.py --ages  # таблица возрастов last_seen (для подбора порогов)

Требование к Zigbee2MQTT (configuration.yaml):
  advanced:
    last_seen: ISO_8601
"""
import html
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import yaml

# ── Настройки ────────────────────────────────────────────────────────────────
Z2M_DIR = os.environ.get("Z2M_DIR", "/config/zigbee2mqtt")
CONFIG_FILE = os.path.join(Z2M_DIR, "configuration.yaml")
STATE_FILE = os.path.join(Z2M_DIR, "state.json")

# Z2M сбрасывает state.json на диск примерно раз в 10 минут (проверьте по mtime).
STATE_MAX_AGE = timedelta(hours=1)

# Порог молчания устройства по умолчанию, часы.
DEFAULT_MAX_AGE_H = 24

# Индивидуальные пороги (часы) по friendly_name или ieee. Пример:
#   "bath_leak_sensor": 12,
# Подбирайте по `--ages`: порог должен быть заметно больше обычного интервала
# между сообщениями устройства, иначе будут ложные тревоги.
MAX_AGE_OVERRIDES_H = {
}

# Не проверять (friendly_name или ieee): снятые с эксплуатации, лежащие в ящике и т.п.
IGNORE = set()

# «Массовый сбой»: молчат >= max(MASS_MIN, MASS_SHARE * всего) устройств сразу.
MASS_MIN = 3
MASS_SHARE = 0.5

MAX_REPORT_LINES = 30  # лимит Telegram — 4096 символов
# ─────────────────────────────────────────────────────────────────────────────


class _Loader(yaml.SafeLoader):
    """SafeLoader, который не падает на тегах вида !secret / !include."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def read_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.load(f, Loader=_Loader)  # noqa: S506 — _Loader наследует SafeLoader
    return data if isinstance(data, dict) else {}


def norm_ieee(key):
    # Неквотированный 0x… YAML читает как int.
    if isinstance(key, int):
        return f"0x{key:016x}"
    return str(key).strip().lower()


def load_devices():
    """{ieee: friendly_name}. Понимает inline-секцию devices и `devices: devices.yaml`."""
    raw = read_yaml(CONFIG_FILE).get("devices")
    parts = []
    if isinstance(raw, dict):
        parts.append(raw)
    elif isinstance(raw, str):
        parts.append(read_yaml(os.path.join(Z2M_DIR, raw)))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                parts.append(read_yaml(os.path.join(Z2M_DIR, item)))
            elif isinstance(item, dict):
                parts.append(item)

    devices = {}
    for part in parts:
        for key, props in part.items():
            props = props if isinstance(props, dict) else {}
            if props.get("disabled"):
                continue
            ieee = norm_ieee(key)
            devices[ieee] = str(props.get("friendly_name") or ieee)
    return {
        ieee: name
        for ieee, name in devices.items()
        if ieee not in IGNORE and name not in IGNORE
    }


def read_state():
    """Читает state.json; Z2M пишет файл не атомарно — при битом JSON повторяем."""
    last_err = None
    for _ in range(3):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("state.json не является JSON-объектом")
            return data
        except json.JSONDecodeError as e:
            last_err = e
            time.sleep(1)
    raise last_err


def parse_ts(value):
    """ISO_8601 / ISO_8601_local / epoch (мс) → aware datetime (UTC)."""
    if isinstance(value, bool):
        raise ValueError(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise ValueError(value)


def limit_for(ieee, name):
    hours = MAX_AGE_OVERRIDES_H.get(name) or MAX_AGE_OVERRIDES_H.get(ieee) or DEFAULT_MAX_AGE_H
    return timedelta(hours=hours)


def fmt_age(td):
    hours = td.total_seconds() / 3600
    if hours >= 48:
        return f"{hours / 24:.1f} сут"
    if hours >= 1:
        return f"{hours:.0f} ч"
    return f"{int(td.total_seconds() // 60)} мин"


def esc(text):
    return html.escape(str(text), quote=False)


def build_report(status, *, problems=(), checked=0, state_age=None, message="", mass=False):
    """Готовый HTML для Telegram (parse_mode: html)."""
    if status == "ok":
        return f"✅ <b>Zigbee: все устройства на связи</b> ({checked})"
    if status == "stale":
        return (
            f"⚠️ <b>Zigbee2MQTT: state.json не обновляется {fmt_age(state_age)}</b>\n"
            "Z2M, вероятно, завис или остановлен. Проверка устройств приостановлена."
        )
    if status == "error":
        return f"🚨 <b>Проверка Zigbee не отработала</b>\n<code>{esc(message)}</code>"

    lines = [f"⚠️ <b>Zigbee: нет связи с устройствами ({len(problems)} из {checked})</b>"]
    if mass:
        lines.append("Похоже на общий сбой (Z2M / координатор / питание), а не на отдельные устройства.")
    for p in problems[:MAX_REPORT_LINES]:
        name = esc(p["name"])
        if p["reason"] == "silent":
            age = fmt_age(timedelta(seconds=p["age_seconds"]))
            lines.append(f"• <b>{name}</b> — {age} назад (порог {p['limit_hours']:g} ч)")
        elif p["reason"] == "no_data":
            lines.append(f"• <b>{name}</b> — нет last_seen в state.json")
        else:
            lines.append(f"• <b>{name}</b> — нечитаемый last_seen: <code>{esc(p['last_seen'])}</code>")
    if len(problems) > MAX_REPORT_LINES:
        lines.append(f"… и ещё {len(problems) - MAX_REPORT_LINES}")
    return "\n".join(lines)


def make_result(status, now, *, state_age=None, checked=0, problems=(), message="", mass=False):
    problems = sorted(problems, key=lambda p: p["name"])
    if status == "offline":
        key = "offline:" + ",".join(sorted(p["ieee"] for p in problems))
    elif status == "error":
        key = "error:" + message[:80]
    else:
        key = status
    return {
        "status": status,
        "message": message,
        "state_age_seconds": int(state_age.total_seconds()) if state_age is not None else None,
        "checked": checked,
        "offline_count": len(problems),
        "offline_names": [p["name"] for p in problems],
        "offline_devices": problems,
        "mass_outage": mass,
        "problem_key": key,
        "report": build_report(
            status, problems=problems, checked=checked,
            state_age=state_age, message=message, mass=mass,
        ),
        "checked_at": now.isoformat(),
    }


def evaluate(now):
    """→ (state_age, devices, problems, rows). Бросает исключение при невозможности проверки."""
    mtime = os.path.getmtime(STATE_FILE)
    state_age = now - datetime.fromtimestamp(mtime, timezone.utc)

    devices = load_devices()
    if not devices:
        raise RuntimeError("в configuration.yaml не найдено ни одного устройства")

    state = read_state()
    problems, rows, has_last_seen = [], [], False
    for ieee, name in devices.items():
        entry = state.get(ieee)
        raw = entry.get("last_seen") if isinstance(entry, dict) else None
        limit = limit_for(ieee, name)
        base = {"name": name, "ieee": ieee, "limit_hours": limit.total_seconds() / 3600}

        if raw is None:
            problems.append({**base, "reason": "no_data", "last_seen": None, "age_seconds": None})
            rows.append((None, base))
            continue
        try:
            age = now - parse_ts(raw)
        except (ValueError, TypeError, OverflowError, OSError):
            problems.append({**base, "reason": "bad_timestamp", "last_seen": str(raw), "age_seconds": None})
            rows.append((None, base))
            continue

        has_last_seen = True
        rows.append((age, base))
        if age > limit:
            problems.append({**base, "reason": "silent", "last_seen": str(raw),
                             "age_seconds": int(age.total_seconds())})

    if not has_last_seen:
        raise RuntimeError(
            "ни у одного устройства нет читаемого last_seen: "
            "включите advanced.last_seen: ISO_8601 в configuration.yaml Zigbee2MQTT"
        )
    return state_age, devices, problems, rows


def run(now):
    try:
        state_age = now - datetime.fromtimestamp(os.path.getmtime(STATE_FILE), timezone.utc)
    except OSError as e:
        return make_result("error", now, message=f"state.json недоступен: {e}")
    if state_age > STATE_MAX_AGE:
        return make_result("stale", now, state_age=state_age)

    state_age, devices, problems, _ = evaluate(now)
    if not problems:
        return make_result("ok", now, state_age=state_age, checked=len(devices))

    silent = sum(1 for p in problems if p["reason"] == "silent")
    mass = silent >= max(MASS_MIN, math.ceil(MASS_SHARE * len(devices)))
    return make_result("offline", now, state_age=state_age, checked=len(devices),
                       problems=problems, mass=mass)


def print_ages(now):
    _, _, _, rows = evaluate(now)
    rows.sort(key=lambda r: (r[0] is None, -(r[0].total_seconds() if r[0] else 0)))
    print(f"{'возраст':>10}  {'порог':>6}  устройство")
    for age, base in rows:
        age_s = fmt_age(age) if age is not None else "нет данных"
        print(f"{age_s:>10}  {base['limit_hours']:>5g}ч  {base['name']}  ({base['ieee']})")


def main():
    now = datetime.now(timezone.utc)
    if "--ages" in sys.argv[1:]:
        print_ages(now)
        return
    try:
        result = run(now)
    except Exception as e:  # noqa: BLE001 — контракт: всегда валидный JSON
        result = make_result("error", now, message=f"{type(e).__name__}: {e}")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

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

Возраст устройства — более свежая из двух меток: lastSeen в database.db
(её пишет zigbee-herdsman) и last_seen в state.json. На этой установке state.json
сохраняет старый last_seen, хотя MQTT уже публикует новый; без database.db
короткий порог даёт ложный общий сбой.

Пороги:
  * state.json старше STATE_MAX_AGE — процесс Z2M не сбрасывает кэш (интервал записи 5 мин);
  * питание mains (розетки и реле) — PROBE_MAX_AGE;
    если молчат все такие устройства сразу, это сеть/координатор, а не одна розетка;
  * в кэше есть water_leak — LEAK_MAX_AGE;
  * остальные — DEFAULT_MAX_AGE_H.
    Сузить точечно — MAX_AGE_OVERRIDES_H после `check_zigbee.py --ages`.

Гистерезиса нет: новое сообщение сбрасывает возраст почти в ноль, а повторные
опросы и так не шлют Telegram, пока не изменился problem_key.
Availability Zigbee2MQTT (ping роутеров) сюда не подмешан: он меняет available
у сущностей HA, а не только эту проверку.
"""
import html
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import yaml

# ── Настройки ────────────────────────────────────────────────────────────────
Z2M_DIR = os.environ.get("Z2M_DIR", "/config/zigbee2mqtt")
CONFIG_FILE = os.path.join(Z2M_DIR, "configuration.yaml")
STATE_FILE = os.path.join(Z2M_DIR, "state.json")
DB_FILE = os.path.join(Z2M_DIR, "database.db")

# Z2M пишет state.json каждые 5 минут, даже если кэш не менялся.
STATE_MAX_AGE = timedelta(minutes=30)

# Порог молчания устройства по умолчанию, часы.
DEFAULT_MAX_AGE_H = 24

# Питание от сети: розетки и реле. 2 часа — зазор над их реальным интервалом
# (на этой сети это минуты), без ложной тревоги на батарейной кнопке.
PROBE_MAX_AGE = timedelta(hours=2)
PROBE_KEYS = {"power", "energy", "current"}
# Датчики протечки на этой сети выходят на связь реже чем за 40 минут.
LEAK_MAX_AGE = timedelta(hours=3)
LEAK_KEYS = {"water_leak"}
# Столько контрольных розеток нужно, чтобы молчание всех сразу считать сетью.
NETWORK_MIN_PROBES = 2

# Индивидуальные пороги (часы) по friendly_name или ieee. Пример:
#   "bath_leak_sensor": 6,
# Ноль допустим. Подбирайте по `--ages`: порог заметно больше обычного интервала.
MAX_AGE_OVERRIDES_H = {
}

# Не проверять (friendly_name или ieee): снятые с эксплуатации, лежащие в ящике и т.п.
IGNORE = set()

# «Массовый сбой» по числу молчащих, если контрольных розеток меньше двух.
MASS_MIN = 3
MASS_SHARE = 0.5

MAX_REPORT_LINES = 30  # лимит Telegram — 4096 символов
DEVICE_TYPES = {"Coordinator", "Router", "EndDevice", "Unknown", "GreenPower"}
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


def power_kind(value):
    """mains | battery | unknown. «Mains (w/ battery backup)» остаётся mains."""
    if isinstance(value, bool) or value is None:
        return "unknown"
    if isinstance(value, (int, float)):
        primary = int(value) & 0x7F
        if primary in (1, 2, 4, 5, 6):
            return "mains"
        if primary == 3:
            return "battery"
        return "unknown"
    if not isinstance(value, str):
        return "unknown"
    text = value.lower().split("(w/")[0]
    if "battery" in text and "mains" not in text and "dc source" not in text:
        return "battery"
    if "mains" in text or "dc source" in text or "emergency mains" in text:
        return "mains"
    return "unknown"


def _remember_meta(found, entry):
    if not isinstance(entry, dict) or entry.get("type") not in DEVICE_TYPES:
        return
    ieee = entry.get("ieeeAddr") or entry.get("ieee")
    if not ieee:
        return
    found[norm_ieee(ieee)] = {
        "type": entry.get("type") or "Unknown",
        "kind": power_kind(entry.get("powerSource")),
        "last_seen": entry.get("lastSeen"),
    }


def load_radio_meta():
    """ieee → {type, kind} из database.db. Пустой словарь, если файла нет или он битый.

    Формат Zigbee2MQTT 2.x — NDJSON (не sqlite). Старый sqlite читается отдельно.
    Классификация необязательна: без неё порог остаётся суточным.
    """
    try:
        with open(DB_FILE, "rb") as f:
            magic = f.read(15)
    except OSError:
        return {}
    try:
        if magic.startswith(b"SQLite format"):
            return _meta_sqlite()
        return _meta_ndjson()
    except (OSError, sqlite3.Error, UnicodeError):
        return {}


def _meta_ndjson():
    found = {}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                _remember_meta(found, json.loads(line))
            except json.JSONDecodeError:
                continue
    return found


def _meta_sqlite():
    found = {}
    uri = "file:" + DB_FILE.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "devices" not in tables:
            return {}
        cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
        if "ieeeAddr" not in cols:
            return {}
        type_col = "type" if "type" in cols else None
        power_col = "powerSource" if "powerSource" in cols else None
        wanted = ["ieeeAddr"] + [c for c in (type_col, power_col) if c]
        for row in conn.execute(f"SELECT {', '.join(wanted)} FROM devices"):
            rec = dict(zip(wanted, row))
            _remember_meta(found, {
                "type": rec.get("type") or "Unknown",
                "ieeeAddr": rec.get("ieeeAddr"),
                "powerSource": rec.get("powerSource"),
            })
    finally:
        conn.close()
    return found


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


def index_state(state):
    """Ключи state.json бывают в разном регистре; группы (маленькие int) пропускаем."""
    indexed = {}
    for key, value in state.items():
        if isinstance(key, str) and key.lower().startswith("0x"):
            indexed[norm_ieee(key)] = value
        elif isinstance(key, int) and key > 0xFFFF:
            indexed[norm_ieee(key)] = value
    return indexed


def parse_ts(value):
    """ISO_8601 / ISO_8601_local / epoch (мс) → aware datetime (UTC).

    Метка без пояса — локальное время контейнера. ISO_8601_local у Z2M именно такое;
    считать его UTC в Екатеринбурге значит прятать отказ лишние 5 часов.
    """
    if isinstance(value, bool):
        raise ValueError(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt.astimezone(timezone.utc)
    raise ValueError(value)


def is_probe(meta, entry):
    """Сетевое устройство. Батарея не пингуется этим порогом.

    `voltage` не смотрим: у батарейных датчиков это милливольты батареи.
    Реле без power/energy/current всё равно сетевое, если database.db говорит mains.
    """
    if meta.get("kind") == "battery":
        return False
    if meta.get("kind") == "mains":
        return True
    if meta.get("type") == "EndDevice":
        return False
    if not isinstance(entry, dict):
        return False
    return bool(PROBE_KEYS & entry.keys())


def limit_for(ieee, name, probe, entry):
    if name in MAX_AGE_OVERRIDES_H:
        hours = MAX_AGE_OVERRIDES_H[name]
    elif ieee in MAX_AGE_OVERRIDES_H:
        hours = MAX_AGE_OVERRIDES_H[ieee]
    elif probe:
        return PROBE_MAX_AGE
    elif isinstance(entry, dict) and (LEAK_KEYS & entry.keys()):
        return LEAK_MAX_AGE
    else:
        hours = DEFAULT_MAX_AGE_H
    return timedelta(hours=hours)


def freshest(state_raw, db_raw):
    """Самая новая читаемая метка. Битые значения не затирают живую вторую метку."""
    good = []
    bad = []
    for raw in (state_raw, db_raw):
        if raw is None:
            continue
        try:
            good.append((parse_ts(raw), raw))
        except (ValueError, TypeError, OverflowError, OSError):
            bad.append(raw)
    if good:
        return max(good, key=lambda item: item[0])
    if bad:
        return None, bad[0]
    return None, None


def fmt_age(td):
    hours = td.total_seconds() / 3600
    if hours >= 48:
        return f"{hours / 24:.1f} сут"
    if hours >= 1:
        return f"{hours:.0f} ч"
    return f"{int(td.total_seconds() // 60)} мин"


def fmt_limit(td):
    hours = td.total_seconds() / 3600
    if hours < 1:
        return f"{int(round(hours * 60))} мин"
    if hours < 48:
        text = f"{hours:g}"
        return f"{text} ч"
    return f"{hours / 24:g} сут"


def esc(text):
    return html.escape(str(text), quote=False)


def build_report(status, *, problems=(), checked=0, state_age=None, message="", mass=False, network=False):
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
    if network:
        lines.append(
            "Контрольные розетки молчат все сразу: похоже на координатор, радио или питание, "
            "а не на одну розетку."
        )
    elif mass:
        lines.append("Похоже на общий сбой (Z2M / координатор / питание), а не на отдельные устройства.")
    for p in problems[:MAX_REPORT_LINES]:
        name = esc(p["name"])
        if p["reason"] == "silent":
            age = fmt_age(timedelta(seconds=p["age_seconds"]))
            lines.append(f"• <b>{name}</b> — {age} назад (порог {fmt_limit(timedelta(hours=p['limit_hours']))})")
        elif p["reason"] == "no_data":
            lines.append(f"• <b>{name}</b> — нет last_seen в state.json")
        else:
            lines.append(f"• <b>{name}</b> — нечитаемый last_seen: <code>{esc(p['last_seen'])}</code>")
    if len(problems) > MAX_REPORT_LINES:
        lines.append(f"… и ещё {len(problems) - MAX_REPORT_LINES}")
    return "\n".join(lines)


def make_result(status, now, *, state_age=None, checked=0, problems=(), message="", mass=False, network=False):
    problems = sorted(problems, key=lambda p: (not p.get("probe"), p["name"]))
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
            state_age=state_age, message=message, mass=mass, network=network,
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

    state = index_state(read_state())
    meta = load_radio_meta()
    problems, rows, has_last_seen = [], [], False
    for ieee, name in devices.items():
        entry = state.get(ieee)
        info = meta.get(ieee, {})
        state_raw = entry.get("last_seen") if isinstance(entry, dict) else None
        seen_at, raw = freshest(state_raw, info.get("last_seen"))
        probe = is_probe(info, entry if isinstance(entry, dict) else None)
        limit = limit_for(ieee, name, probe, entry if isinstance(entry, dict) else None)
        base = {
            "name": name,
            "ieee": ieee,
            "limit_hours": limit.total_seconds() / 3600,
            "probe": probe,
            "kind": info.get("kind") or "unknown",
        }

        if seen_at is None and raw is None:
            problems.append({**base, "reason": "no_data", "last_seen": None, "age_seconds": None})
            rows.append((None, base))
            continue
        if seen_at is None:
            problems.append({**base, "reason": "bad_timestamp", "last_seen": str(raw), "age_seconds": None})
            rows.append((None, base))
            continue

        has_last_seen = True
        age = now - seen_at
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


def network_down(rows):
    """Все контрольные розетки молчат дольше своего порога. Одной мало: это ещё не сеть.

    Порог берётся из записи устройства, чтобы MAX_AGE_OVERRIDES_H мог его поднять.
    """
    probes = [(age, base) for age, base in rows if base.get("probe")]
    if len(probes) < NETWORK_MIN_PROBES:
        return False
    return all(
        age is None or age > timedelta(hours=base["limit_hours"])
        for age, base in probes
    )


def run(now):
    try:
        state_age = now - datetime.fromtimestamp(os.path.getmtime(STATE_FILE), timezone.utc)
    except OSError as e:
        return make_result("error", now, message=f"state.json недоступен: {e}")
    if state_age > STATE_MAX_AGE:
        return make_result("stale", now, state_age=state_age)

    state_age, devices, problems, rows = evaluate(now)
    network = network_down(rows)
    if not problems:
        return make_result("ok", now, state_age=state_age, checked=len(devices))

    silent = sum(1 for p in problems if p["reason"] == "silent")
    mass = network or silent >= max(MASS_MIN, math.ceil(MASS_SHARE * len(devices)))
    return make_result(
        "offline", now, state_age=state_age, checked=len(devices),
        problems=problems, mass=mass, network=network,
    )


def emit(text):
    """UTF-8 в stdout. Локаль cp1251/ascii не должна ронять сенсор на эмодзи отчёта."""
    data = text.encode("utf-8")
    if not data.endswith(b"\n"):
        data += b"\n"
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def print_ages(now):
    state_age, _, _, rows = evaluate(now)
    rows.sort(key=lambda r: (r[0] is None, -(r[0].total_seconds() if r[0] else 0)))
    lines = [f"state.json: {fmt_age(state_age)}", f"{'возраст':>10}  {'порог':>8}  {'роль':<10}  устройство"]
    for age, base in rows:
        age_s = fmt_age(age) if age is not None else "нет данных"
        role = "розетка" if base.get("probe") else base.get("kind") or "unknown"
        limit = fmt_limit(timedelta(hours=base["limit_hours"]))
        lines.append(f"{age_s:>10}  {limit:>8}  {role:<10}  {base['name']}  ({base['ieee']})")
    emit("\n".join(lines))


def main():
    now = datetime.now(timezone.utc)
    if "--ages" in sys.argv[1:]:
        print_ages(now)
        return
    try:
        result = run(now)
    except Exception as e:  # noqa: BLE001 — контракт: всегда валидный JSON
        result = make_result("error", now, message=f"{type(e).__name__}: {e}")
    emit(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

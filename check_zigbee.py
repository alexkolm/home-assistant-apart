import json
import os
from datetime import datetime, timezone, timedelta


STATE_FILE = "/config/zigbee2mqtt/state.json"
CONFIG_FILE = "/config/zigbee2mqtt/configuration.yaml"
STATUS_FILE = "/config/zigbee_check_status.json"

DEVICE_MAX_AGE = timedelta(hours=24)
STATE_MAX_AGE = timedelta(hours=1)


def save_status(status, state_age=None, offline_devices=None):

    if offline_devices is None:
        offline_devices = []

    data = {
        "status": status,
        "state_age_seconds": (
            int(state_age.total_seconds())
            if state_age is not None
            else None
        ),
        "offline_count": len(offline_devices),
        "offline_devices": offline_devices,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    tmp_file = STATUS_FILE + ".tmp"

    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(tmp_file, STATUS_FILE)


def load_devices():

    devices = {}

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    current_ieee = None

    for line in lines:

        stripped = line.strip()

        if stripped.startswith("'0x") and stripped.endswith("':"):
            current_ieee = stripped[1:-2]

        elif current_ieee and stripped.startswith("friendly_name:"):

            devices[current_ieee] = stripped.split(
                ":",
                1
            )[1].strip()

            current_ieee = None

    return devices


def check_state_file(now):

    try:
        mtime = os.path.getmtime(STATE_FILE)

    except OSError as e:

        print(
            f"STATE_ERROR | cannot access state.json: {e}"
        )

        return False, None

    modified = datetime.fromtimestamp(
        mtime,
        timezone.utc
    )

    age = now - modified

    if age > STATE_MAX_AGE:

        return False, age

    return True, age


def check_devices(state, devices, now):

    offline = []

    for ieee, name in devices.items():

        data = state.get(ieee, {})

        last_seen = data.get("last_seen")

        if not last_seen:
            continue

        try:

            dt = datetime.fromisoformat(
                last_seen.replace("Z", "+00:00")
            )

        except ValueError:

            continue

        age = now - dt

        if age > DEVICE_MAX_AGE:

            offline.append({
                "name": name,
                "ieee": ieee,
                "last_seen": last_seen,
                "age_seconds": int(
                    age.total_seconds()
                )
            })

    return offline


def main():

    now = datetime.now(timezone.utc)

    # Проверяем свежесть state.json

    state_ok, state_age = check_state_file(now)

    if not state_ok:

        if state_age is None:

            save_status("error")

            print(
                "STATE_ERROR | state.json unavailable"
            )

        else:

            save_status(
                "stale",
                state_age
            )

            print(
                f"STATE_STALE | state.json age={state_age}"
            )

        return

    # Читаем state.json

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

    except Exception as e:

        save_status(
            "error",
            state_age
        )

        print(
            f"STATE_ERROR | cannot read state.json: {e}"
        )

        return

    # Читаем список устройств

    try:

        devices = load_devices()

    except Exception as e:

        save_status(
            "error",
            state_age
        )

        print(
            f"CONFIG_ERROR | cannot read "
            f"configuration.yaml: {e}"
        )

        return

    # Проверяем устройства

    offline = check_devices(
        state,
        devices,
        now
    )

    if offline:

        save_status(
            "offline",
            state_age,
            offline
        )

        for device in offline:

            print(
                f"OFFLINE "
                f"{device['name']} "
                f"({device['ieee']}) "
                f"last_seen={device['last_seen']} "
                f"age={timedelta(seconds=device['age_seconds'])}"
            )

    else:

        save_status(
            "ok",
            state_age,
            []
        )

        print("OK")


if __name__ == "__main__":
    main()
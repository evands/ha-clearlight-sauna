#!/usr/bin/env python3
"""Export one sauna session's raw history from Home Assistant to CSV.

Home Assistant keeps full-resolution history for about 10 days (the
recorder's purge_keep_days), so export each calibration run within that.

    HA_URL=http://homeassistant.local:8123 HA_TOKEN=... \\
        python3 export_session.py 2026-10-12T21:45 2026-10-12T22:55 data/2026-10-12-max.csv

Times are local unless they carry an offset. The token is a long-lived access
token (your HA profile -> Security). Output is long-form, one row per state
change: ``time,entity_id,state``. Climate rows carry hvac_action and the
target as ``state|hvac_action|target``.

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime

CABIN = "sensor.clearlight_sauna_cabin_temperature"
CLIMATE = "climate.clearlight_sauna"
# The heater relay sensor exists when the integration's upgrade-heater option is on.
RELAY = "binary_sensor.master_bathroom_clearlight_sauna_heater_relay"


def _iso(text: str) -> str:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # local time
    return dt.isoformat()


def _get(base: str, token: str, path: str, params: dict[str, str]) -> list:
    url = f"{base.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    start, end, out = _iso(argv[1]), _iso(argv[2]), argv[3]
    base = os.environ["HA_URL"]
    token = os.environ["HA_TOKEN"]
    path = "/api/history/period/" + urllib.parse.quote(start)
    common = {"end_time": end, "significant_changes_only": "0"}

    rows: list[tuple[str, str, str]] = []
    plain = _get(base, token, path, {**common, "filter_entity_id": f"{CABIN},{RELAY}",
                                     "minimal_response": "1", "no_attributes": "1"})
    for series in plain:
        entity = series[0]["entity_id"] if series else None
        for st in series:
            rows.append((st.get("last_changed") or st["last_updated"], st.get("entity_id", entity), st["state"]))
    for series in _get(base, token, path, {**common, "filter_entity_id": CLIMATE}):
        for st in series:
            attrs = st.get("attributes", {})
            value = f"{st['state']}|{attrs.get('hvac_action', '')}|{attrs.get('temperature', '')}"
            rows.append((st["last_updated"], CLIMATE, value))

    rows.sort(key=lambda r: datetime.fromisoformat(r[0]))
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "entity_id", "state"])
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

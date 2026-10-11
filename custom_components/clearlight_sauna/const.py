"""Constants for the Clearlight Sauna integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "clearlight_sauna"

CONF_MAC = "mac"
CONF_DID = "did"

# Fallback poll in case push reports stop; the device normally pushes changes.
WATCHDOG_INTERVAL = timedelta(seconds=60)

# Keypad limits from Clearlight's app documentation: max 175 °F, sessions up
# to 60 minutes in 5-minute steps. The minimum setpoint isn't documented. The
# controller stores whatever it is sent (verified), so these bounds are ours.
MIN_TEMP_F = 70
MAX_TEMP_F = 175
SESSION_MIN_MINUTES = 5
SESSION_MAX_MINUTES = 60
SESSION_STEP_MINUTES = 5

#!/bin/sh
export PYTHONPATH=/app/lib/dogmute PYTHONUNBUFFERED=1 HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_VERBOSITY=error
exec python3 -m dogmute.flatpak "$@"

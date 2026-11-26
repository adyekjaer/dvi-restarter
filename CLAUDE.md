# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A watchdog that keeps a DVI heat pump running: it power-cycles the pump (full master off/on) when it reports an error, and turns it back on if it is found off. Deployed with Docker Compose on a Raspberry Pi (arm64, `docker-compose` v1 syntax there) and also runnable on x86.

- `dvi_watchdog.py` – the long-running loop (container entrypoint).
- `dvi_restarter.py` – `DVIPortal` client for the SmartControl website, plus a manual CLI (`status|on|off|restart`). The watchdog imports `DVIPortal` from here.

## Commands

```bash
pip install -r requirements.txt                 # only dependency: requests
python3 dvi_restarter.py status                  # read-only; needs DVI_USER + DVI_PASS(WORD)
LOG_LEVEL=DEBUG python3 dvi_restarter.py restart --off-time 60 --confirm-timeout 120
python3 dvi_watchdog.py                          # needs DVI_USER, DVI_PASS, DVI_FABNR
docker compose up -d --build && docker compose logs -f dvi-watchdog   # Pi: docker-compose
```

All configuration is environment variables (read in `Settings` in `dvi_watchdog.py`; defaults mirrored in `docker-compose.yml`). Credentials live in an untracked `.env`. `DRY_RUN=1` logs actions without sending on/off commands.

There is no test suite or linter. Logic has been verified by importing `dvi_watchdog`, replacing `dvi_watchdog.DVIPortal` with a fake class, `read_heatpump_error` with a stub returning `(error, sensor_date)`, and `time.sleep` with a no-op, then calling `Watchdog(Settings()).check()` repeatedly. Do not run anything that sends on/off commands against the real pump without the user's explicit go-ahead.

## Two DVI backends (non-obvious)

**Public API** – `POST https://ws.dvienergi.com/API/` with a JSON body containing credentials + `fabnr` (pump serial). Stateless. Used only for reading `output.bestgreen`:
- `Heatpump.Error`: string, `"0"` = healthy.
- `Sensor.Date`: **UTC**, updated roughly every 60 s, also while the pump is switched off.
- The API can neither switch the master on/off nor report the on/off state (its `set` only covers CH/VV settings).

**SmartControl website** – server-rendered PHP, session cookie, no pump ID in requests (the session determines the pump). Everything goes through `POST /includes/process.php`:
- Login: form fields `user`, `pass`, `sublogin=1` → 302 to `main.php`.
- State: `subupdatemainbuttons=1` returns HTML containing `turnPumpOnOff(N)`. The button offers the *opposite* state: `4` ⇒ pump is ON, `2` ⇒ pump is OFF.
- Command: `subusersetting=1&setting=onoff&value=4` (off) / `value=2` (on); body `1` = accepted.
- The command request blocks until the pump acknowledges (observed 15 s off, 58 s on), hence `COMMAND_TIMEOUT = 180`. A read timeout is treated as "unknown, verify via `wait_for_state()`", not as failure – the command usually took effect.

## Watchdog check order (deliberate, keep it)

1. API: if `Sensor.Date` is older than `MAX_DATA_AGE` (or unparseable / in the future) → do nothing this round, not even log in to the portal.
2. Portal: if pump is OFF → `ensure_on()`.
3. If `Heatpump.Error` not in `HEALTHY_ERROR_VALUES` → power cycle, unless within `RESTART_GRACE` of the last restart or in backoff (`MAX_RESTARTS` within `RESTART_WINDOW` triggers `BACKOFF`). Turning the pump on is never subject to backoff.

## Invariant: never leave the pump off

Once an OFF command is sent, `power_cycle()` always ends in `ensure_on()` (in a `finally`), which retries indefinitely and ignores the stop event; SIGTERM/SIGINT only stop the loop between checks. `stop_grace_period: 35m` in compose exists so Docker doesn't kill the container mid-cycle. Preserve this when changing anything in the restart path.

## Secrets

Credentials only come from env / `.env`. HAR captures and API dumps contain the password or serial and are gitignored (`*.har`, `api_*.json`); the DEBUG log level prints the login email.

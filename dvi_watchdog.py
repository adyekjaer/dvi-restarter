#!/usr/bin/env python3
"""Watchdog for a DVI heat pump: keep it on, power-cycle it when it reports an error.

Every CHECK_INTERVAL seconds:
  1. Read bestgreen from the public API. If Sensor.Date is older than
     MAX_DATA_AGE the pump is not reporting -> do nothing this round.
  2. Log in to the SmartControl portal and check the master on/off state.
     If the pump is OFF -> turn it ON (also during backoff).
  3. If the pump is ON and Heatpump.Error is an error value -> power-cycle the
     pump (off, hold, on), unless too many restarts happened recently, in
     which case restarts are paused.

Safety rule: the pump must never be left off. Once an OFF command has been
sent, the script keeps trying to turn it back on, also when asked to shut down
(SIGTERM/SIGINT are honoured only after the pump is confirmed ON again).

All settings are environment variables, see SETTINGS below.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import requests

from dvi_restarter import DVIPortal, DVIPortalError

API_URL = 'https://ws.dvienergi.com/API/'

log = logging.getLogger('dvi-watchdog')
stop_event = threading.Event()


def env_str(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f'Missing required environment variable {name}')
    return value


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        sys.exit(f'Environment variable {name} must be an integer')


class Settings:
    def __init__(self):
        self.user = env_str('DVI_USER', required=True)
        self.password = env_str('DVI_PASSWORD') or env_str('DVI_PASS', required=True)
        self.fabnr = env_str('DVI_FABNR', required=True)
        # Seconds between checks.
        self.check_interval = env_int('CHECK_INTERVAL', 300)
        # Seconds to wait for the portal to confirm an on/off command.
        self.confirm_timeout = env_int('CONFIRM_TIMEOUT', 120)
        # Attempts per on/off command before giving up on that step.
        self.command_attempts = env_int('COMMAND_ATTEMPTS', 3)
        # Seconds the pump is held off during a power cycle.
        self.off_hold = env_int('OFF_HOLD', 60)
        # Seconds after a power cycle during which errors are ignored, so the
        # API has time to report fresh data and the pump time to start up.
        self.restart_grace = env_int('RESTART_GRACE', 600)
        # Max age in seconds of the API data (bestgreen.Sensor.Date, which is
        # UTC and normally updated every ~60 s). Older data means the pump is
        # not reporting, so the error value cannot be trusted: no restarts.
        self.max_data_age = env_int('MAX_DATA_AGE', 120)
        # Backoff: if MAX_RESTARTS power cycles happen within RESTART_WINDOW
        # seconds, stop power-cycling for BACKOFF seconds.
        self.max_restarts = env_int('MAX_RESTARTS', 3)
        self.restart_window = env_int('RESTART_WINDOW', 3600)
        self.backoff = env_int('BACKOFF', 3600)
        # Heatpump.Error values that mean "healthy" (comma separated).
        self.healthy_values = {
            v.strip() for v in env_str('HEALTHY_ERROR_VALUES', '0').split(',') if v.strip()}
        # Log what would be done, but never send on/off commands.
        self.dry_run = env_str('DRY_RUN', '0').lower() in ('1', 'true', 'yes')
        # File touched after every successful check (used by the Docker healthcheck).
        self.heartbeat_file = env_str('HEARTBEAT_FILE', '/tmp/dvi-watchdog.heartbeat')
        self.log_level = env_str('LOG_LEVEL', 'INFO').upper()


def read_heatpump_error(settings):
    """Return (error_value_as_str, sensor_date) from the public API."""
    payload = {
        'usermail': settings.user,
        'userpassword': settings.password,
        'fabnr': settings.fabnr,
        'get': {'bestgreen': 1},
    }
    resp = requests.post(API_URL, data=json.dumps(payload), timeout=(5, 30))
    resp.raise_for_status()
    data = resp.json()
    log.debug('API -> HTTP %d: %s', resp.status_code,
              {k: v for k, v in data.items() if k != 'output'} if isinstance(data, dict) else data)
    if not isinstance(data, dict) or data.get('Access') != 'Granted':
        raise RuntimeError(f'API access not granted ({data.get("Access") if isinstance(data, dict) else data!r})')
    bestgreen = data.get('output', {}).get('bestgreen', {})
    log.debug('API bestgreen: %s', bestgreen)
    if 'Heatpump.Error' not in bestgreen:
        raise RuntimeError('bestgreen.Heatpump.Error missing from API response')
    return str(bestgreen['Heatpump.Error']).strip(), bestgreen.get('Sensor.Date')


def now_in_backoff(watchdog):
    return time.monotonic() < watchdog.backoff_until


def data_age_seconds(sensor_date):
    """Age in seconds of a bestgreen Sensor.Date ('YYYY-mm-dd HH:MM:SS', UTC)."""
    reported = datetime.strptime(sensor_date, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - reported).total_seconds()


class Watchdog:
    def __init__(self, settings):
        self.s = settings
        self.restarts = deque()        # monotonic timestamps of power cycles
        self.last_restart = None       # monotonic timestamp of last power cycle
        self.backoff_until = 0.0       # monotonic timestamp

    # ---- portal helpers -------------------------------------------------

    def portal(self):
        """Fresh logged-in portal session (sessions may expire between checks)."""
        portal = DVIPortal(self.s.user, self.s.password)
        portal.login()
        log.debug('Logged in to portal')
        return portal

    def command(self, want_on):
        """Send on/off up to COMMAND_ATTEMPTS times, each confirmed within CONFIRM_TIMEOUT.

        Returns True when the portal confirms the wanted state.
        """
        word = 'ON' if want_on else 'OFF'
        for attempt in range(1, self.s.command_attempts + 1):
            try:
                portal = self.portal()
                if portal.is_on() == want_on:
                    log.info('Pump confirmed %s', word)
                    return True
                if self.s.dry_run:
                    log.warning('DRY_RUN: would turn pump %s', word)
                    return True
                log.info('Turning pump %s (attempt %d/%d)', word, attempt, self.s.command_attempts)
                portal.set_power(want_on)
                if portal.wait_for_state(want_on, self.s.confirm_timeout):
                    log.info('Pump confirmed %s', word)
                    return True
                log.warning('Pump not confirmed %s within %ds', word, self.s.confirm_timeout)
            except (requests.RequestException, DVIPortalError) as err:
                log.warning('Turning pump %s failed: %s', word, err)
            time.sleep(10)
        log.error('Could not confirm pump %s after %d attempts', word, self.s.command_attempts)
        return False

    def ensure_on(self):
        """Keep trying to turn the pump on until confirmed. Ignores shutdown requests."""
        delay = 30
        while not self.command(True):
            log.error('PUMP MAY BE OFF - retrying in %ds', delay)
            time.sleep(delay)
            delay = min(delay * 2, 300)

    # ---- main logic -----------------------------------------------------

    def power_cycle(self):
        now = time.monotonic()
        self.restarts.append(now)
        self.last_restart = now
        log.warning('Power-cycling pump to clear error (restart %d within the last %d min)',
                    self.restarts_in_window(), self.s.restart_window // 60)

        # From here on the pump must end up ON, whatever happens.
        try:
            if not self.command(False):
                log.error('Pump did not confirm OFF; turning it back ON without holding')
            else:
                log.info('Holding pump off for %ds', self.s.off_hold)
                time.sleep(self.s.off_hold)
        finally:
            self.ensure_on()
        log.info('Power cycle complete after %.0fs', time.monotonic() - now)

    def restarts_in_window(self):
        cutoff = time.monotonic() - self.s.restart_window
        while self.restarts and self.restarts[0] < cutoff:
            self.restarts.popleft()
        return len(self.restarts)

    def check(self):
        # 1. Is the pump reporting at all? Without fresh data nothing is done,
        #    not even turning it on: commands would not reach it anyway.
        error, sensor_date = read_heatpump_error(self.s)
        try:
            age = data_age_seconds(sensor_date)
        except (TypeError, ValueError):
            log.warning('Cannot parse Sensor.Date %r - doing nothing this round', sensor_date)
            return
        # A large negative age means clock trouble on one side; don't trust it either.
        if age > self.s.max_data_age or age < -self.s.max_data_age:
            log.warning('API data is stale: Sensor.Date %s UTC is %ds old (max %ds) - pump may '
                        'not be reporting, doing nothing this round (Heatpump.Error=%s)',
                        sensor_date, age, self.s.max_data_age, error)
            return

        # 2. Pump must be on.
        portal = self.portal()
        is_on = portal.is_on()
        log.debug('Portal reports pump %s', 'ON' if is_on else 'OFF')
        if not is_on:
            log.warning('Pump is OFF - turning it ON (data %ds old)', age)
            self.ensure_on()
            return

        # 3. Pump is on and reporting: check for errors.
        if self.restarts_in_window() or now_in_backoff(self):
            log.info('Restarts in last %d min: %d%s', self.s.restart_window // 60,
                     self.restarts_in_window(),
                     f', backoff for another {int(self.backoff_until - time.monotonic()) // 60} min'
                     if now_in_backoff(self) else '')
        if error in self.s.healthy_values:
            log.info('Pump ON, healthy (Heatpump.Error=%s, data %ds old)', error, age)
            return

        now = time.monotonic()
        log.warning('Pump ON, ERROR (Heatpump.Error=%s, data %ds old)', error, age)

        if self.last_restart is not None and now - self.last_restart < self.s.restart_grace:
            log.info('Within %ds grace period after last restart - not restarting yet',
                     self.s.restart_grace)
            return
        if now < self.backoff_until:
            log.warning('In backoff for another %d min - not restarting',
                        (self.backoff_until - now) // 60)
            return
        if self.restarts_in_window() >= self.s.max_restarts:
            self.backoff_until = now + self.s.backoff
            log.error('%d restarts within %d min did not clear the error - backing off for %d min',
                      self.s.max_restarts, self.s.restart_window // 60, self.s.backoff // 60)
            return

        self.power_cycle()

    def run(self):
        log.info('Starting watchdog with settings: %s', ', '.join(
            f'{k}={v!r}' for k, v in sorted(vars(self.s).items()) if k != 'password'))
        while not stop_event.is_set():
            try:
                self.check()
                Path(self.s.heartbeat_file).touch()
            except (requests.RequestException, DVIPortalError, RuntimeError, ValueError) as err:
                log.error('Check failed: %s', err)
            except Exception:  # keep the loop alive no matter what
                log.exception('Unexpected error during check')
            stop_event.wait(self.s.check_interval)
        log.info('Stopped')


def main():
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    )
    # Keep library noise down even at DEBUG.
    logging.getLogger('urllib3').setLevel(logging.INFO)

    def handle_signal(signum, _frame):
        log.info('Received signal %d, stopping after current check', signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    Watchdog(settings).run()


if __name__ == '__main__':
    main()

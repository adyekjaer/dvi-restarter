#!/usr/bin/env python3
"""Turn a DVI heat pump fully off/on via the SmartControl web portal.

The public API (ws.dvienergi.com/API) only exposes CH/VV, not the master
on/off switch. The web portal does, through an undocumented session-based
endpoint, which this script mimics:

    POST /includes/process.php  user=..&pass=..&sublogin=1      -> login (302)
    POST /includes/process.php  subupdatemainbuttons=1          -> HTML with turnPumpOnOff(N)
    POST /includes/process.php  subusersetting=1&setting=onoff&value=4  -> off ("1" = ok)
    POST /includes/process.php  subusersetting=1&setting=onoff&value=2  -> on  ("1" = ok)

The button always offers the opposite of the current state, so
turnPumpOnOff(4) in the page means the pump is ON, turnPumpOnOff(2) means OFF.

Credentials are read from DVI_USER / DVI_PASSWORD (or DVI_PASS) (preferred, keeps the
password out of `ps`) or from --username / --password.
"""

import argparse
import logging
import os
import re
import sys
import time

import requests

BASE_URL = 'https://smartcontrol.dvienergi.com'
PROCESS_URL = f'{BASE_URL}/includes/process.php'

VALUE_ON = 2
VALUE_OFF = 4

# The portal holds the on/off request open until the pump acknowledges it
# (observed 15 s for off, 58 s for on), so it needs a much longer timeout.
COMMAND_TIMEOUT = 180

log = logging.getLogger('dvi-portal')


class DVIPortalError(RuntimeError):
    """Raised when the portal does not respond as expected."""


class DVIPortal:
    """Minimal client for the SmartControl web portal."""

    def __init__(self, username, password, timeout=30):
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers['User-Agent'] = (
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')

    def _ajax(self, data, timeout=None):
        """POST to process.php the same way the page's jQuery does."""
        started = time.monotonic()
        log.debug('POST process.php %s', data)
        resp = self.session.post(
            PROCESS_URL,
            data=data,
            headers={
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': f'{BASE_URL}/main.php',
                'Origin': BASE_URL,
            },
            timeout=(10, timeout or self.timeout),
        )
        log.debug('-> HTTP %d in %.1fs, %d bytes: %.200r', resp.status_code,
                  time.monotonic() - started, len(resp.text), resp.text)
        resp.raise_for_status()
        return resp.text

    def login(self):
        log.debug('Logging in to portal as %s', self.username)
        # Get an initial PHPSESSID like a browser would.
        self.session.get(f'{BASE_URL}/', timeout=self.timeout).raise_for_status()
        resp = self.session.post(
            PROCESS_URL,
            data={
                'user': self.username,
                'pass': self.password,
                'sublogin': '1',
            },
            headers={'Referer': f'{BASE_URL}/', 'Origin': BASE_URL},
            allow_redirects=False,
            timeout=self.timeout,
        )
        location = resp.headers.get('Location', '')
        log.debug('Login -> HTTP %d, Location=%r', resp.status_code, location)
        if resp.status_code != 302 or 'main.php' not in location:
            raise DVIPortalError(
                f'Login failed (status {resp.status_code}, Location={location!r})')
        # Verify the session actually works.
        self.is_on()

    def is_on(self):
        """Return True if the pump is on, False if off."""
        html = self._ajax({'subupdatemainbuttons': '1'})
        match = re.search(r'turnPumpOnOff\((\d+)\)', html)
        if not match:
            raise DVIPortalError(
                'Could not find on/off state in portal response '
                '(not logged in, or the portal changed)')
        offered = int(match.group(1))
        log.debug('Portal on/off button offers turnPumpOnOff(%d)', offered)
        if offered == VALUE_OFF:
            return True
        if offered == VALUE_ON:
            return False
        raise DVIPortalError(f'Unknown on/off button value {offered}')

    def set_power(self, on, timeout=COMMAND_TIMEOUT):
        """Send the on/off command.

        A read timeout is not treated as failure: the command has usually
        reached the pump already, so callers must verify with wait_for_state().
        """
        value = VALUE_ON if on else VALUE_OFF
        word = 'on' if on else 'off'
        started = time.monotonic()
        try:
            result = self._ajax({
                'subusersetting': '1',
                'setting': 'onoff',
                'value': str(value),
            }, timeout=timeout).strip()
        except requests.exceptions.ReadTimeout:
            log.warning('Portal did not answer the %s command within %ds; '
                        'verifying state instead', word, timeout)
            return
        log.info('Portal answered %s command with %r after %.1fs',
                 word, result, time.monotonic() - started)
        if result != '1':
            raise DVIPortalError(
                f'Portal rejected {"on" if on else "off"} command (response {result!r})')

    def wait_for_state(self, on, timeout, interval=5):
        """Poll until the portal reports the wanted state, or time out."""
        started = time.monotonic()
        deadline = started + timeout
        while True:
            current = self.is_on()
            log.debug('Waiting for %s: portal says %s (%.0fs elapsed)',
                      state_str(on), state_str(current), time.monotonic() - started)
            if current == on:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)


def state_str(on):
    return 'ON' if on else 'OFF'


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('action', choices=['status', 'on', 'off', 'restart'])
    parser.add_argument('-u', '--username', default=os.environ.get('DVI_USER'),
                        help='Portal login email (default: $DVI_USER)')
    parser.add_argument('-p', '--password',
                        default=os.environ.get('DVI_PASSWORD') or os.environ.get('DVI_PASS'),
                        help='Portal password (default: $DVI_PASSWORD or $DVI_PASS)')
    parser.add_argument('--off-time', type=int, default=60,
                        help='Seconds to keep the pump off during restart (default: 60)')
    parser.add_argument('--confirm-timeout', type=int, default=60,
                        help='Seconds to wait for the portal to report a new state (default: 60)')
    args = parser.parse_args(argv)
    if not args.username or not args.password:
        parser.error('username and password required (DVI_USER/DVI_PASSWORD or -u/-p)')
    return args


def main(argv):
    args = parse_args(argv)
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
                        format='%(asctime)s %(levelname)s %(message)s')
    portal = DVIPortal(args.username, args.password)
    portal.login()

    current = portal.is_on()
    print(f'Pump is currently {state_str(current)}')

    if args.action == 'status':
        return 0

    if args.action in ('on', 'off'):
        want = args.action == 'on'
        if current == want:
            print(f'Already {state_str(want)}, nothing to do')
            return 0
        portal.set_power(want)
        ok = portal.wait_for_state(want, args.confirm_timeout)
        print(f'Turned {state_str(want)}' + ('' if ok else ' (portal has not confirmed yet)'))
        return 0 if ok else 1

    # restart
    if current:
        print('Turning pump OFF')
        portal.set_power(False)
        if not portal.wait_for_state(False, args.confirm_timeout):
            print('Warning: portal has not confirmed OFF state', file=sys.stderr)
        print(f'Waiting {args.off_time}s')
        time.sleep(args.off_time)
    else:
        print('Pump was already OFF, just turning it ON')

    print('Turning pump ON')
    # Retry the ON command a few times: leaving the pump off is the worst outcome.
    for attempt in range(1, 4):
        try:
            portal.set_power(True)
            if portal.wait_for_state(True, args.confirm_timeout):
                print('Pump is ON again')
                return 0
            print(f'Attempt {attempt}: portal has not confirmed ON', file=sys.stderr)
        except (requests.RequestException, DVIPortalError) as err:
            print(f'Attempt {attempt}: turning ON failed ({err})', file=sys.stderr)
            try:
                portal.login()
            except (requests.RequestException, DVIPortalError) as login_err:
                print(f'Re-login failed ({login_err})', file=sys.stderr)
        time.sleep(10)

    print('ERROR: could not confirm the pump is back ON - check it manually!', file=sys.stderr)
    return 2


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except (requests.RequestException, DVIPortalError) as err:
        print(f'ERROR: {err}', file=sys.stderr)
        sys.exit(1)

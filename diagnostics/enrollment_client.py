"""Supplied first-use team sign-in. Credentials never belong in student Git.

No password is retained. Windows credentials use user-scoped DPAPI. The
existing evaluator receives its credential only through the child environment.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = 'ap-classroom-device-v1'


class EnrollmentError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def base_url(value):
    try:
        p = urlsplit(value)
        if (p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password
                or p.query or p.fragment or p.path not in ('', '/')
                or p.scheme == 'http' and p.hostname not in ('localhost', '127.0.0.1', '::1')):
            raise ValueError
    except (TypeError, ValueError):
        raise EnrollmentError('ACCOUNT_SERVER', 'Use the class HTTPS address.') from None
    return value.rstrip('/')


def binding(root, server):
    # A different checkout is never silently rebound to another team's queue.
    path = os.path.normcase(str(Path(root).resolve()))
    return hashlib.sha256((base_url(server) + '\0' + path).encode('utf-8')).hexdigest()


def repository_identity(root):
    """Bind saved queues to the same origin, without treating it as login proof."""
    root = Path(root).resolve()
    value = 'no-git\0' + os.path.normcase(str(root))
    if (root / '.git').exists():
        try:
            result = subprocess.run(['git', '-C', str(root), 'config', '--get', 'remote.origin.url'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
            if result.returncode not in (0, 1):
                raise ValueError
            remote = result.stdout.decode('utf-8').strip()
            if remote.startswith('git@github.com:'):
                remote = 'https://github.com/' + remote.split(':', 1)[1]
            parsed = urlsplit(remote)
            if parsed.hostname and parsed.hostname.lower() == 'github.com':
                path = parsed.path.rstrip('/')
                if path.lower().endswith('.git'):
                    path = path[:-4]
                remote = 'github.com/' + path.strip('/').casefold()
            value = 'git-origin\0' + remote
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise EnrollmentError('ACCOUNT_REPOSITORY', 'The team repository identity could not be checked. Keep the local results and retry.') from None
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def credential_path(root, server):
    if os.name == 'nt':
        folder = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local')))
    else:
        folder = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share')))
    result = folder / 'AppliedProgramming/2026-02/accounts' / (binding(root, server) + '.json')
    if result.resolve().is_relative_to(Path(root).resolve()):
        raise EnrollmentError('ACCOUNT_STORAGE', 'Account storage must be outside the project repository.')
    return result


def canonical_name(name):
    return ' '.join(unicodedata.normalize('NFKC', name).split()).casefold()


@contextmanager
def account_lock(root, server):
    path = credential_path(root, server).with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'0')
            stream.flush()
        started = time.monotonic()
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - started >= 15:
                    raise EnrollmentError('TEAM_SIGN_IN_BUSY', 'Another command is signing in to this team. Retry after it finishes.') from None
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _dpapi(raw, *, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        method = library.CryptUnprotectData
        method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    else:
        method = library.CryptProtectData
        method.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise EnrollmentError('ACCOUNT_STORAGE', 'Windows could not protect or unlock this account on this user profile.')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _read(root, server):
    path = credential_path(root, server)
    try:
        with path.open('rb') as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError
        data = json.loads(raw)
        if (data.get('schema_version') != VERSION or data.get('binding') != binding(root, server)
                or data.get('server') != base_url(server)):
            raise ValueError
        if data.get('repository_identity') != repository_identity(root):
            raise EnrollmentError('TEAM_REPOSITORY_CHANGED',
                'This checkout was connected with a different Git origin. Keep its outputs and use a separate checkout for the other team.')
        protected = base64.b64decode(data['credential'], validate=True)
        if data['protection'] == 'windows-dpapi' and os.name == 'nt':
            token = _dpapi(protected, decrypt=True).decode('ascii')
        elif data['protection'] == 'user-file' and os.name != 'nt':
            token = protected.decode('ascii')
        else:
            raise ValueError
        if not token.startswith('apd_') or len(token) != 47 or any(c.isspace() for c in token):
            raise ValueError
        return data, token
    except FileNotFoundError:
        return None, ''
    except EnrollmentError:
        raise
    except (OSError, ValueError, KeyError, UnicodeError):
        raise EnrollmentError('ACCOUNT_STORAGE', 'The local team sign-in could not be read. Keep your run outputs and sign in again.') from None


def _save(root, server, data, token):
    protected = _dpapi(token.encode('ascii')) if os.name == 'nt' else token.encode('ascii')
    value = {**data, 'schema_version': VERSION, 'server': base_url(server),
             'binding': binding(root, server), 'repository_identity': repository_identity(root),
             'protection': 'windows-dpapi' if os.name == 'nt' else 'user-file',
             'credential': base64.b64encode(protected).decode('ascii')}
    _write(credential_path(root, server), value)


def stored_account(root, server):
    data, token = _read(root, server)
    if not data or not data.get('team_id'):
        return None
    return {'server': base_url(server), 'token_file': None, 'token': token,
            'team_id': data['team_id'], 'team_name': data['team_name'], 'automatic_account': True}


def request(server, path, *, payload=None, token=None):
    headers = {'Accept': 'application/json', 'ngrok-skip-browser-warning': 'true'}
    raw = None
    if payload is not None:
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(base_url(server) + path, data=raw, headers=headers, method='POST' if raw else 'GET')
    try:
        with build_opener(NoRedirect()).open(req, timeout=10) as response:
            body = response.read(16385)
        if len(body) > 16384:
            raise ValueError
        value = json.loads(body)
        if type(value) is not dict:
            raise ValueError
        return value
    except HTTPError as error:
        # Never echo a remote body, password or credential into diagnostics.
        if error.code in (401, 403, 409):
            raise EnrollmentError('TEAM_SIGN_IN_REFUSED', 'Team sign-in was refused. Use the exact team name and shared password agreed with your teammates.') from None
        if error.code == 429:
            raise EnrollmentError('TEAM_SIGN_IN_BUSY', 'Too many sign-in attempts. Wait before trying again.') from None
        raise EnrollmentError('TEAM_SIGN_IN_UNAVAILABLE', 'Team sign-in is unavailable. Local work can continue. Retry when the class server is online.') from None
    except (OSError, ValueError):
        raise EnrollmentError('TEAM_SIGN_IN_UNAVAILABLE', 'Team sign-in is unavailable. Local work can continue. Retry when the class server is online.') from None


def connect(root, server, team_name, password, *, new_device=False):
    with account_lock(root, server):
        return _connect_locked(root, server, team_name, password, new_device=new_device)


def _connect_locked(root, server, team_name, password, *, new_device=False):
    if (not isinstance(team_name, str) or not 1 <= len(' '.join(unicodedata.normalize('NFKC', team_name).split())) <= 60
            or any(unicodedata.category(c).startswith('C') for c in team_name)):
        raise EnrollmentError('TEAM_NAME_INVALID', 'Use an agreed team name of 1 to 60 printable characters.')
    if (not isinstance(password, str) or len(password) < 12 or len(password.encode('utf-8')) > 256
            or not password.strip() or any(unicodedata.category(c).startswith('C') for c in password)):
        raise EnrollmentError('TEAM_PASSWORD_LENGTH', 'Use a printable team password of at least 12 characters and at most 256 UTF-8 bytes.')
    data, token = _read(root, server)
    if data and data.get('team_name'):
        # Changing accounts must not silently attach pending runs to a new team.
        if canonical_name(data['team_name']) != canonical_name(team_name):
            raise EnrollmentError('TEAM_ALREADY_BOUND', 'This checkout is already linked to another team. Keep its outputs and use a separate team checkout.')
    expected_id = (data or {}).get('team_id') or (data or {}).get('expected_team_id')
    if not token or new_device:
        token = 'apd_' + secrets.token_urlsafe(32)
        data = {'team_name': team_name}
        if expected_id:
            data['expected_team_id'] = expected_id
        # Save before the POST. A lost response reuses the exact same device.
        _save(root, server, data, token)
    answer = request(server, '/api/enrollment/connect', payload={
        'team_name': team_name, 'password': password, 'device_token': token})
    if (answer.get('status') not in ('connected', 'registered')
            or type(answer.get('team_id')) is not str or not answer['team_id']
            or type(answer.get('team_name')) is not str):
        raise EnrollmentError('TEAM_SIGN_IN_ACK', 'The server did not acknowledge this team sign-in.')
    if expected_id and answer['team_id'] != expected_id:
        raise EnrollmentError('TEAM_ID_CHANGED', 'This checkout must remain linked to its original team. Keep its local results.')
    _save(root, server, {'team_id': answer['team_id'], 'team_name': answer['team_name']}, token)
    return stored_account(root, server)


def ensure_account(root, server, *, interactive=None, read_name=input, read_password=getpass.getpass, write=print, force=False):
    account = stored_account(root, server)
    if account and not force:
        return account
    data, token = _read(root, server)
    if token and not force:
        # Recover an acknowledged server-side registration after a lost response.
        try:
            answer = request(server, '/api/enrollment/identity', token=token)
            if type(answer.get('team_id')) is str and type(answer.get('team_name')) is str:
                if data.get('expected_team_id') and data['expected_team_id'] != answer['team_id']:
                    raise EnrollmentError('TEAM_ID_CHANGED', 'Keep this checkout linked to its original team.')
                _save(root, server, {'team_id': answer['team_id'], 'team_name': answer['team_name']}, token)
                return stored_account(root, server)
        except EnrollmentError:
            pass
    if interactive is None:
        interactive = bool(sys.stdin and sys.stdin.isatty())
    if not interactive:
        raise EnrollmentError('TEAM_SIGN_IN_REQUIRED', 'Run a project command once in Anaconda Prompt to sign in to your team. Local results are kept.')
    if force and data and data.get('team_name'):
        name = data['team_name']
        write('Reconnect this checkout to team: ' + name)
    else:
        write('First use: enter the SAME team name and shared password on each teammate\'s computer.')
        write('A new name creates a team. Use a unique agreed name. Do not use your GitHub password.')
        name = read_name('Team name: ').strip()
    password = read_password('Team password (at least 12 characters, hidden): ')
    if len(password) < 12 or len(password.encode('utf-8')) > 256:
        raise EnrollmentError('TEAM_PASSWORD_LENGTH', 'Use a team password of at least 12 characters and at most 256 UTF-8 bytes.')
    account = connect(root, server, name, password, new_device=force)
    write('TEAM_CONNECTED: ' + account['team_name'] + '. Future runs upload automatically.')
    return account

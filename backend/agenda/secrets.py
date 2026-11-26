"""Account secrets: the system keyring first, a 0600 file otherwise.

The keyring is reached through `secret-tool` (libsecret). The secret goes in
on stdin, never on the command line, so it does not show up in `ps`.
accounts.json only ever holds the marker "@keyring" or "@file".
"""

import os
import shutil
import subprocess

from . import storage

ATTR_SERVICE = "dadoux01.agenda"
KEYRING = "@keyring"
FILE = "@file"
SECRETS_FILE = "secrets.json"


def _secret_tool():
    return shutil.which("secret-tool")


def _run(args, stdin=None):
    return subprocess.run(
        args, input=stdin, capture_output=True, text=True, timeout=15, check=False
    )


def store(account_id, secret):
    """Save `secret`; return the marker to keep in accounts.json."""
    tool = _secret_tool()
    if tool:
        try:
            res = _run(
                [tool, "store", "--label=Agenda (" + account_id + ")",
                 "service", ATTR_SERVICE, "account", account_id],
                stdin=secret,
            )
            if res.returncode == 0:
                _file_delete(account_id)
                return KEYRING
        except (OSError, subprocess.SubprocessError):
            pass
    _file_store(account_id, secret)
    return FILE


def load(account_id, marker):
    if marker == KEYRING:
        tool = _secret_tool()
        if not tool:
            return None
        try:
            res = _run([tool, "lookup", "service", ATTR_SERVICE, "account", account_id])
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            return None
        return res.stdout.rstrip("\n") or None
    if marker == FILE:
        data = storage.read_json(storage.state_path(SECRETS_FILE), {})
        value = data.get(account_id) if isinstance(data, dict) else None
        return value if isinstance(value, str) else None
    return None


def delete(account_id):
    tool = _secret_tool()
    if tool:
        try:
            _run([tool, "clear", "service", ATTR_SERVICE, "account", account_id])
        except (OSError, subprocess.SubprocessError):
            pass
    _file_delete(account_id)


_MISSING = object()


def _file_store(account_id, secret):
    path = storage.state_path(SECRETS_FILE)
    data = storage.read_json(path, _MISSING)
    if data is _MISSING:
        if os.path.exists(path) or os.path.islink(path):
            raise storage.StorageError("corrupt_secrets")
        data = {}
    elif not isinstance(data, dict):
        raise storage.StorageError("corrupt_secrets")
    data[account_id] = secret
    storage.write_json(path, data)


def _file_delete(account_id):
    path = storage.state_path(SECRETS_FILE)
    data = storage.read_json(path, None)
    if isinstance(data, dict) and account_id in data:
        del data[account_id]
        storage.write_json(path, data)

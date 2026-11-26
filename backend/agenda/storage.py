"""Where the plugin keeps its files, and how it writes them.

Config (no secrets):  $XDG_CONFIG_HOME/dadoux01.agenda/
State and cache:      $XDG_STATE_HOME/dadoux01.agenda/

Both directories are 0700 and must belong to the current user and not be
symlinks; files are written 0600, atomically, never through a symlink.
"""

import json
import os
import stat
import tempfile

APP = "dadoux01.agenda"


def _base(env, fallback):
    value = os.environ.get(env, "")
    if value and os.path.isabs(value):
        return value
    return os.path.join(os.path.expanduser("~"), fallback)


def config_dir():
    return os.path.join(_base("XDG_CONFIG_HOME", ".config"), APP)


def state_dir():
    return os.path.join(_base("XDG_STATE_HOME", ".local/state"), APP)


class StorageError(Exception):
    pass


def ensure_dir(path):
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except OSError as exc:
        raise StorageError("cannot create " + path + ": " + str(exc))
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise StorageError(path + " is not a plain directory")
        if st.st_uid != os.getuid():
            raise StorageError(path + " is not owned by the current user")
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return path


def read_json(path, default):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return default
    except OSError:
        return default
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode):
            return default
        if st.st_size > 32 * 1024 * 1024:
            return default
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = None
            return json.load(fh)
    except (ValueError, OSError):
        return default
    finally:
        if fd is not None:
            os.close(fd)


def write_json(path, data):
    directory = ensure_dir(os.path.dirname(path))
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def config_path(name):
    return os.path.join(config_dir(), name)


def state_path(name):
    return os.path.join(state_dir(), name)

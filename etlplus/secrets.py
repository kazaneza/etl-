"""Secrets store: connection passwords and tokens, encrypted at rest with Fernet.

The file is <project>/secrets.json.enc. The key comes from $ETLPLUS_SECRET_KEY, or else from
<project>/.secret.key, created once with mode 0600. There is no plain-text fallback.
Values are never logged and never returned by the API (it returns has()).
"""

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

FILE = "secrets.json.enc"
KEY_FILE = ".secret.key"
ENV_KEY = "ETLPLUS_SECRET_KEY"


def _root(project) -> Path:
    return Path(getattr(project, "path", project))


def _fernet(root: Path) -> Fernet:
    env = os.environ.get(ENV_KEY, "").strip()
    if env:
        key = env.encode()
        try:
            return Fernet(key)
        except (ValueError, TypeError):
            # any passphrase works; it is stretched to a valid Fernet key
            return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key).digest()))
    kf = root / KEY_FILE
    if not kf.exists():
        root.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        try:
            fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
        except FileExistsError:
            pass
        try:
            os.chmod(kf, 0o600)
        except OSError:
            pass
    return Fernet(kf.read_bytes().strip())


def _load(root: Path) -> dict:
    f = root / FILE
    if not f.exists():
        return {}
    try:
        data = _fernet(root).decrypt(f.read_bytes())
    except InvalidToken:
        raise RuntimeError(
            "The secrets file cannot be decrypted with this key. "
            f"Check {ENV_KEY} or {KEY_FILE}.") from None
    return json.loads(data.decode("utf-8"))


def _save(root: Path, data: dict) -> None:
    f = root / FILE
    tmp = root / (FILE + ".tmp")
    token = _fernet(root).encrypt(json.dumps(data, sort_keys=True).encode("utf-8"))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(token)
    os.replace(tmp, f)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass


def get(project, cid: str) -> dict:
    return dict(_load(_root(project)).get(cid) or {})


def set(project, cid: str, values: dict) -> None:
    root = _root(project)
    data = _load(root)
    cur = dict(data.get(cid) or {})
    for k, v in (values or {}).items():
        if v is None or v == "":
            cur.pop(k, None)
        else:
            cur[k] = str(v)
    if cur:
        data[cid] = cur
    else:
        data.pop(cid, None)
    _save(root, data)


def update(project, cid: str, values: dict, clear: bool = False) -> None:
    """Set several secrets of one connection in one write. clear=True drops every stored one
    first (the endpoint changed: a saved key is never sent to a new server)."""
    root = _root(project)
    data = _load(root)
    cur = {} if clear else dict(data.get(cid) or {})
    for k, v in (values or {}).items():
        if v is None or v == "":
            cur.pop(k, None)
        else:
            cur[k] = str(v)
    if cur:
        data[cid] = cur
    else:
        data.pop(cid, None)
    _save(root, data)


def delete(project, cid: str) -> None:
    root = _root(project)
    data = _load(root)
    if cid in data:
        data.pop(cid)
        _save(root, data)


def has(project, cid: str) -> dict:
    return {k: bool(v) for k, v in get(project, cid).items()}


def rename(project, old: str, new: str) -> None:
    root = _root(project)
    data = _load(root)
    if old in data:
        data[new] = data.pop(old)
        _save(root, data)

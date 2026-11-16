"""Path agreement check for the loopback panel, not an authorization credential."""
import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path


HEADER = "X-StarWish-Workspace-Check"
_FORMAT = re.compile(r"([0-9a-f]{32})\.([0-9a-f]{64})")


def _digest(path: Path, nonce: str) -> str:
    canonical = os.path.normcase(str(path.resolve())).encode("utf-8", "surrogatepass")
    return hashlib.sha256(b"starwish-workspace-v1\x00" + nonce.encode("ascii") + b"\x00" + canonical).hexdigest()


def new_claim(path: Path) -> str:
    nonce = secrets.token_hex(16)
    return nonce + "." + _digest(path, nonce)


def matches(path: Path, claim: str) -> bool:
    parts = _FORMAT.fullmatch(claim)
    return parts is not None and hmac.compare_digest(_digest(path, parts[1]), parts[2])

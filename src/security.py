"""Private-key loading + format validation.

Two sources, in order of precedence:
1. macOS Keychain (service ``polymarket-bot``, account ``private-key``) —
   shared with the polymarket trade bot at ~/polymarket_trade_bot so a
   single Keychain entry covers both bots.  Setup is the trade bot's
   responsibility (``python main.py --setup-keychain`` over there); this
   module is read-only.
2. ``ETH_PRIVATE_KEY`` environment variable (typically from .env) —
   fallback for non-macOS environments (VPS paper deploys, CI).

If neither resolves to a 64-hex-char key, ``load_eth_private_key()``
raises ``RuntimeError`` with a copy-pasteable Keychain setup hint.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "polymarket-bot"
KEYCHAIN_ACCOUNT = "private-key"


def _check_format(key: str) -> bool:
    """Return True iff ``key`` is a valid 64-hex-char Ethereum private key."""
    clean = key.strip().lower()
    if clean.startswith("0x"):
        clean = clean[2:]
    if len(clean) != 64:
        return False
    try:
        int(clean, 16)
    except ValueError:
        return False
    return True


def _fingerprint(key: str) -> str:
    h = hashlib.sha256(key.encode()).hexdigest()
    return f"sha256:{h[:8]}...{h[-4:]}"


def load_key_from_keychain() -> str | None:
    """Read the private key from macOS Keychain, or return None.

    Returns None on non-macOS, when the Keychain entry is missing, or when
    the stored value fails format validation — callers fall back to env.
    Raises only on programmer error (subprocess module unavailable etc.),
    not on the expected "no entry" path.
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        return None
    key = result.stdout.strip()
    if key and _check_format(key):
        logger.info(
            "Private key loaded from macOS Keychain (%s)", _fingerprint(key),
        )
        return key
    if key:
        logger.warning(
            "Keychain entry %s/%s is present but does not parse as a "
            "64-hex-char private key — falling back to environment.",
            KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT,
        )
    return None


def load_eth_private_key() -> str:
    """Return the Ethereum private key for live trading.

    Order: macOS Keychain → ``ETH_PRIVATE_KEY`` env → raise.  The env
    fallback exists so paper / dry-run runs on a VPS (no Keychain) still
    work — paper mode never actually signs, but the executor reads the
    key once at startup, so it has to load *something*.
    """
    key = load_key_from_keychain()
    if key:
        return key

    env_key = os.environ.get("ETH_PRIVATE_KEY", "").strip()
    if env_key and _check_format(env_key):
        logger.info(
            "Private key loaded from ETH_PRIVATE_KEY env (%s)",
            _fingerprint(env_key),
        )
        return env_key

    raise RuntimeError(
        "No private key available. On macOS, store one in Keychain:\n"
        "  security add-generic-password -s {svc} -a {acct} -w '0x...your_key...'\n"
        "Or set ETH_PRIVATE_KEY in .env (less secure — plaintext on disk).\n"
        "Use --paper or --dry-run if you only need simulated trading.".format(
            svc=KEYCHAIN_SERVICE, acct=KEYCHAIN_ACCOUNT,
        ),
    )

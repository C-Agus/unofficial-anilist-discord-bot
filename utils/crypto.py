"""Encryption helpers for data stored at rest.

Design notes
------------
AniList usernames are stored two ways in the database:

* ``anilist_username_encrypted`` — Fernet (AES-128-CBC + HMAC) ciphertext.
  This is reversible so the bot can display the name and query the API.
  Fernet output is randomized per encryption, so it can *not* be used for
  equality/uniqueness checks.
* ``anilist_username_hash`` — a deterministic, keyed HMAC-SHA256 fingerprint
  of the case-folded username. Because it is deterministic it can carry a
  ``UNIQUE`` constraint, and because it is keyed with the secret it cannot be
  reversed with a simple dictionary of AniList usernames by someone who only
  has the database file.

Both derive from the single ``ENCRYPTION_KEY`` environment variable, so setup
stays a one-key affair. If the key is lost or rotated, stored usernames can no
longer be decrypted and users simply need to re-link.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(RuntimeError):
    """Raised when encryption/decryption fails (bad or rotated key, etc.)."""


class SecretBox:
    """Encrypts values at rest and produces deterministic fingerprints."""

    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key)
            self._hmac_key = base64.urlsafe_b64decode(key)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise CryptoError(
                "ENCRYPTION_KEY is not a valid Fernet key. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` and return a urlsafe token string."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Decrypt a token produced by :meth:`encrypt`."""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise CryptoError(
                "Could not decrypt a stored value. The ENCRYPTION_KEY has likely "
                "changed since the value was stored."
            ) from exc

    def fingerprint(self, value: str) -> str:
        """Deterministic keyed fingerprint used for uniqueness checks.

        Case-insensitive: ``"Foo"`` and ``"foo"`` produce the same fingerprint,
        matching AniList's own case-insensitive username handling.
        """
        normalized = value.strip().casefold().encode("utf-8")
        return hmac.new(
            self._hmac_key, b"anilist-username:" + normalized, hashlib.sha256
        ).hexdigest()

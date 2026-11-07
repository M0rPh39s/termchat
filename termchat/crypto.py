# End-to-end encryption for termchat.
#
# Every user owns an ECDH P-256 identity keypair. The private key never
# leaves the client in the clear: it is stored (locally and, for multi-device
# restore, on the server) as a PKCS8 blob wrapped with AES-256-GCM under a key
# derived by PBKDF2 from a SEPARATE "encryption passphrase" that is never sent
# to the server. The login password - which the server does see - is not used
# for any encryption key, so the server can never unwrap the private key.
#
# Messages use hybrid encryption. Each message gets a random 256-bit content
# key; the plaintext is sealed with AES-256-GCM under it. The content key is
# then ECIES-wrapped (ephemeral ECDH -> HKDF -> AES-256-GCM) to every channel
# member's public key, so only those members can unwrap it. The server only
# ever stores and relays the resulting ciphertext envelope.
#
# All primitives (ECDH P-256, HKDF-SHA256, AES-256-GCM, PBKDF2-HMAC-SHA256,
# uncompressed-point / PKCS8 key encodings) are chosen to interoperate byte
# for byte with the browser's WebCrypto implementation in web.py.

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Marker prefixing the stored "content" of every encrypted message. Anything
# without it is treated as legacy plaintext.
ENVELOPE_PREFIX = "TCE1:"

_CURVE = ec.SECP256R1()
_PBKDF2_ITERATIONS = 200_000
_HKDF_INFO = b"termchat-wrap-v1"


# -- small base64 helpers ----------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text)


# -- public-key helpers ------------------------------------------------------

def _load_public(raw: bytes) -> ec.EllipticCurvePublicKey:
    return ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, raw)


def public_fingerprint(public_b64: str) -> str:
    """Short, stable id for a public key (so a recipient can find its own
    wrapped-key entry in an envelope). First 16 urlsafe-base64 chars of the
    SHA-256 of the raw 65-byte uncompressed point."""
    digest = hashlib.sha256(_b64d(public_b64)).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")[:16]


class Identity:
    """A user's ECDH P-256 keypair plus the operations that need the private
    key. Construct via :meth:`generate` (new user) or :meth:`unlock` (restore
    an existing key from its wrapped blob)."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey):
        self._priv = private_key
        pub = private_key.public_key()
        self._public_raw = pub.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        self.public_b64 = _b64e(self._public_raw)
        self.fingerprint = public_fingerprint(self.public_b64)

    # -- construction --------------------------------------------------------

    @classmethod
    def generate(cls) -> "Identity":
        return cls(ec.generate_private_key(_CURVE))

    @classmethod
    def unlock(cls, enc_private_b64: str, salt_hex: str,
               passphrase: str) -> "Identity":
        """Recover an identity from its wrapped PKCS8 blob. Raises on a wrong
        passphrase or corrupt blob."""
        wrap_key = _derive_wrap_key(passphrase, bytes.fromhex(salt_hex))
        blob = _b64d(enc_private_b64)
        nonce, ct = blob[:12], blob[12:]
        pkcs8 = AESGCM(wrap_key).decrypt(nonce, ct, None)
        priv = serialization.load_der_private_key(pkcs8, password=None)
        return cls(priv)

    # -- private-key backup --------------------------------------------------

    def wrap_private_key(self, passphrase: str):
        """Return ``(enc_private_b64, salt_hex)`` for storing this identity's
        private key, encrypted under ``passphrase``."""
        salt = os.urandom(16)
        wrap_key = _derive_wrap_key(passphrase, salt)
        pkcs8 = self._priv.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        nonce = os.urandom(12)
        ct = AESGCM(wrap_key).encrypt(nonce, pkcs8, None)
        return _b64e(nonce + ct), salt.hex()

    # -- message decryption --------------------------------------------------

    def decrypt(self, content: str):
        """Decrypt an envelope string. Returns the plaintext, or ``None`` if
        this identity is not a recipient / the envelope is malformed."""
        if not is_encrypted(content):
            return content
        try:
            env = json.loads(_b64d(content[len(ENVELOPE_PREFIX):]))
            slot = env["r"].get(self.fingerprint)
            if slot is None:
                return None
            epk = _load_public(_b64d(env["epk"]))
            shared = self._priv.exchange(ec.ECDH(), epk)
            wrap_key = _hkdf(shared, _b64d(env["epk"]))
            content_key = AESGCM(wrap_key).decrypt(
                _b64d(slot["iv"]), _b64d(slot["k"]), None)
            plaintext = AESGCM(content_key).decrypt(
                _b64d(env["iv"]), _b64d(env["ct"]), None)
            return plaintext.decode("utf-8")
        except Exception:
            return None


# -- key derivation ----------------------------------------------------------

def _derive_wrap_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, _PBKDF2_ITERATIONS, 32)


def _hkdf(shared_secret: bytes, salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=_HKDF_INFO).derive(shared_secret)


# -- message encryption ------------------------------------------------------

def is_encrypted(content) -> bool:
    return isinstance(content, str) and content.startswith(ENVELOPE_PREFIX)


def encrypt(plaintext: str, recipients) -> str:
    """Seal ``plaintext`` for ``recipients`` (an iterable of public-key base64
    strings) and return the ``TCE1:`` envelope string.

    Raises ValueError if there are no usable recipients."""
    pubs = []
    seen = set()
    for pub_b64 in recipients:
        if not pub_b64:
            continue
        fp = public_fingerprint(pub_b64)
        if fp in seen:
            continue
        seen.add(fp)
        try:
            pubs.append((fp, _load_public(_b64d(pub_b64)), _b64d(pub_b64)))
        except Exception:
            continue  # skip a malformed key rather than fail the whole send
    if not pubs:
        raise ValueError("no valid recipients to encrypt to")

    content_key = os.urandom(32)
    msg_nonce = os.urandom(12)
    ct = AESGCM(content_key).encrypt(msg_nonce, plaintext.encode("utf-8"), None)

    ephemeral = ec.generate_private_key(_CURVE)
    epk_raw = ephemeral.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )

    slots = {}
    for fp, pub, _raw in pubs:
        shared = ephemeral.exchange(ec.ECDH(), pub)
        wrap_key = _hkdf(shared, epk_raw)
        wrap_nonce = os.urandom(12)
        wrapped = AESGCM(wrap_key).encrypt(wrap_nonce, content_key, None)
        slots[fp] = {"iv": _b64e(wrap_nonce), "k": _b64e(wrapped)}

    env = {"v": 1, "epk": _b64e(epk_raw), "iv": _b64e(msg_nonce),
           "ct": _b64e(ct), "r": slots}
    return ENVELOPE_PREFIX + _b64e(json.dumps(env, separators=(",", ":"))
                                   .encode("utf-8"))

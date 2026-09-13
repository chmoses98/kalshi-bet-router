"""Authentication behaviour, asserted without ever revealing key material."""

from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

from kalshi_router.auth import (
    HEADER_KEY_ID,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    KalshiSigner,
    build_signature_message,
    redacted_header_names,
)
from kalshi_router.config import read_credentials
from kalshi_router.errors import ConfigurationError, CredentialError

from .synthetic import FAKE_KEY_ID


def test_signature_message_is_timestamp_method_path():
    assert build_signature_message(1703123456789, "get", "/trade-api/v2/portfolio/fills") == (
        "1703123456789GET/trade-api/v2/portfolio/fills"
    )


def test_signature_message_excludes_query_string():
    message = build_signature_message(1, "GET", "/trade-api/v2/portfolio/fills?limit=100&cursor=abc")
    assert message == "1GET/trade-api/v2/portfolio/fills"


def test_signature_message_requires_absolute_path():
    with pytest.raises(CredentialError):
        build_signature_message(1, "GET", "portfolio/fills")


def test_signature_verifies_under_rsa_pss_sha256(signer, fake_private_key_pem):
    timestamp, method, path = 1703123456789, "GET", "/trade-api/v2/portfolio/fills"
    signature = base64.b64decode(signer.sign(timestamp, method, path))
    public_key = serialization.load_pem_private_key(
        fake_private_key_pem.encode(), password=None
    ).public_key()
    public_key.verify(
        signature,
        build_signature_message(timestamp, method, path).encode("ascii"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
        hashes.SHA256(),
    )


def test_signature_does_not_verify_for_a_different_path(signer, fake_private_key_pem):
    signature = base64.b64decode(signer.sign(1, "GET", "/trade-api/v2/portfolio/fills"))
    public_key = serialization.load_pem_private_key(
        fake_private_key_pem.encode(), password=None
    ).public_key()
    with pytest.raises(InvalidSignature):
        public_key.verify(
            signature,
            build_signature_message(1, "GET", "/trade-api/v2/markets/OTHER").encode("ascii"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
            hashes.SHA256(),
        )


def test_headers_contain_the_three_documented_names(signer):
    headers = signer.headers("GET", "/trade-api/v2/portfolio/fills", timestamp_ms=42)
    assert set(headers) == {HEADER_KEY_ID, HEADER_TIMESTAMP, HEADER_SIGNATURE}
    assert headers[HEADER_TIMESTAMP] == "42"
    assert headers[HEADER_KEY_ID] == FAKE_KEY_ID


def test_signer_repr_and_str_never_reveal_key_material(signer, fake_private_key_pem):
    for rendered in (repr(signer), str(signer), f"{signer}"):
        assert "redacted" in rendered
        assert FAKE_KEY_ID not in rendered
        assert "BEGIN" not in rendered.upper()
        assert fake_private_key_pem.splitlines()[1] not in rendered


def test_redacted_header_names_returns_names_only(signer):
    headers = signer.headers("GET", "/trade-api/v2/portfolio/fills")
    names = redacted_header_names(headers)
    assert names == sorted([HEADER_KEY_ID, HEADER_SIGNATURE, HEADER_TIMESTAMP])
    assert headers[HEADER_SIGNATURE] not in " ".join(names)


# --------------------------------------------------------------- PEM handling

def test_accepts_multiline_pem_as_stored_in_a_github_secret(fake_private_key_pem):
    assert KalshiSigner(FAKE_KEY_ID, fake_private_key_pem).sign(1, "GET", "/x")


def test_accepts_pem_with_escaped_newlines(fake_private_key_pem):
    escaped = fake_private_key_pem.replace("\n", "\\n")
    assert KalshiSigner(FAKE_KEY_ID, escaped).sign(1, "GET", "/x")


def test_accepts_base64_wrapped_pem(fake_private_key_pem):
    wrapped = base64.b64encode(fake_private_key_pem.encode()).decode()
    assert KalshiSigner(FAKE_KEY_ID, wrapped).sign(1, "GET", "/x")


def test_rejects_non_rsa_key():
    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    with pytest.raises(CredentialError, match="RSA"):
        KalshiSigner(FAKE_KEY_ID, pem)


def test_rejects_garbage_and_does_not_echo_it():
    # Header assembled at runtime so the repository contains no PEM header text.
    dash = "-" * 5
    secret_looking = (
        f"{dash}BEGIN PRIVATE KEY{dash}\nSUPERSECRETGARBAGE\n{dash}END PRIVATE KEY{dash}"
    )
    with pytest.raises(CredentialError) as excinfo:
        KalshiSigner(FAKE_KEY_ID, secret_looking)
    assert "SUPERSECRETGARBAGE" not in str(excinfo.value)


def test_rejects_empty_key_id(fake_private_key_pem):
    with pytest.raises(CredentialError):
        KalshiSigner("   ", fake_private_key_pem)


# ------------------------------------------------------- credential discovery

def test_missing_both_credentials_fails_closed():
    with pytest.raises(ConfigurationError) as excinfo:
        read_credentials(env={})
    assert "KALSHI_API_KEY_ID" in str(excinfo.value)
    assert "KALSHI_PRIVATE_KEY" in str(excinfo.value)


@pytest.mark.parametrize("missing", ["KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY"])
def test_each_missing_credential_fails_closed(missing, fake_private_key_pem):
    env = {"KALSHI_API_KEY_ID": FAKE_KEY_ID, "KALSHI_PRIVATE_KEY": fake_private_key_pem}
    env.pop(missing)
    with pytest.raises(ConfigurationError, match=missing):
        read_credentials(env=env)


def test_blank_credentials_are_treated_as_missing(fake_private_key_pem):
    with pytest.raises(ConfigurationError):
        read_credentials(env={"KALSHI_API_KEY_ID": "  ", "KALSHI_PRIVATE_KEY": "\n\n"})


def test_credential_error_message_never_contains_the_key(fake_private_key_pem):
    with pytest.raises(ConfigurationError) as excinfo:
        read_credentials(env={"KALSHI_API_KEY_ID": "", "KALSHI_PRIVATE_KEY": fake_private_key_pem})
    assert "BEGIN" not in str(excinfo.value).upper()
    assert fake_private_key_pem.splitlines()[1] not in str(excinfo.value)

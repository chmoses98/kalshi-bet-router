from __future__ import annotations

import pytest

from kalshi_router.auth import KalshiSigner
from kalshi_router.config import AuditConfig

from .synthetic import FAKE_KEY_ID, generate_fake_private_key_pem


@pytest.fixture(scope="session")
def fake_private_key_pem() -> str:
    return generate_fake_private_key_pem()


@pytest.fixture()
def signer(fake_private_key_pem: str) -> KalshiSigner:
    return KalshiSigner(FAKE_KEY_ID, fake_private_key_pem)


@pytest.fixture()
def config() -> AuditConfig:
    # No retry sleeping in tests; retries are exercised with an injected sleep.
    return AuditConfig(max_fills=500, page_limit=100, timeout_seconds=1.0, max_retries=2)

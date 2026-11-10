"""Credential encryption round-trips and tolerates legacy plaintext rows."""

from agent_platform.core.credentials import decrypt_secret, encrypt_secret


def test_encrypt_roundtrip():
    ciphertext = encrypt_secret("super-secret")
    assert ciphertext is not None
    assert ciphertext.startswith("enc:v1:")
    assert ciphertext != "super-secret"
    assert decrypt_secret(ciphertext) == "super-secret"


def test_encrypt_is_idempotent_for_ciphertext():
    ciphertext = encrypt_secret("value")
    assert encrypt_secret(ciphertext) == ciphertext


def test_legacy_plaintext_stays_readable():
    assert decrypt_secret("plain-legacy-value") == "plain-legacy-value"


def test_empty_values_pass_through():
    assert encrypt_secret(None) is None
    assert decrypt_secret(None) is None
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""

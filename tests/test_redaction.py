from __future__ import annotations

from winfirst.core.redaction import (
    redact_identifiers,
    redact_mapping,
    redact_secrets,
    should_redact_env_name,
)


def test_empty_input_is_empty() -> None:
    assert redact_secrets("") == ""
    assert redact_secrets(None) == ""


def test_password_value_is_redacted() -> None:
    text = "user=alice password=hunter2 more=text"
    result = redact_secrets(text)
    assert "hunter2" not in result
    assert "***REDACTED***" in result
    assert "more=text" in result


def test_bearer_token_is_redacted() -> None:
    result = redact_secrets("Authorization: Bearer abc.def-123_ghi")
    assert "abc.def-123_ghi" not in result
    assert result == "Authorization: ***REDACTED***"


def test_product_key_is_redacted() -> None:
    result = redact_secrets("ProductKey=AAAAA-BBBBB-CCCCC-DDDDD-EEEEE")
    assert "AAAAA-BBBBB" not in result


def test_command_line_style_secret_is_redacted() -> None:
    result = redact_secrets("setup.exe --password swordfish --quiet")
    assert "swordfish" not in result
    assert "--quiet" in result


def test_environment_names_that_look_sensitive() -> None:
    assert should_redact_env_name("GITHUB_TOKEN")
    assert should_redact_env_name("MyApiKey")
    assert should_redact_env_name("DB_PASSWORD")
    assert should_redact_env_name("AWS_SESSION_TOKEN")
    assert should_redact_env_name("DATABASE_CONNECTION_STRING")
    assert should_redact_env_name("SSH_PASSPHRASE")
    assert should_redact_env_name("AUTH")
    assert not should_redact_env_name("SESSIONNAME")
    assert not should_redact_env_name("PATH")
    assert not should_redact_env_name("TEMP")


def test_credentials_embedded_in_urls_are_redacted() -> None:
    result = redact_secrets("postgresql://alice:hunter2@db.example.local/app")
    assert "hunter2" not in result
    assert "alice" in result
    assert result == "postgresql://alice:***REDACTED***@db.example.local/app"


def test_redact_mapping_redacts_values_by_key() -> None:
    result = redact_mapping(
        [
            ("PATH", r"C:\Windows"),
            ("API_TOKEN", "secret-value"),
            ("NOTE", "token=abc"),
        ]
    )
    assert result["PATH"] == r"C:\Windows"
    assert result["API_TOKEN"] == "***REDACTED***"
    assert "abc" not in result["NOTE"]


def test_identifiers_are_redacted() -> None:
    text = "host TEST-HOST user tester ip 192.168.1.20 mac AA:BB:CC:DD:EE:FF"
    result = redact_identifiers(text, hostname="TEST-HOST", username="tester")
    assert "TEST-HOST" not in result
    assert "tester" not in result
    assert "192.168.1.20" not in result
    assert "AA:BB:CC:DD:EE:FF" not in result
    assert "<HOSTNAME>" in result
    assert "<USER>" in result
    assert "<IP>" in result
    assert "<MAC>" in result


def test_short_identifiers_are_ignored_to_avoid_over_redaction() -> None:
    text = "The host is ab and the user is x"
    assert redact_identifiers(text, hostname="ab", username="x") == text

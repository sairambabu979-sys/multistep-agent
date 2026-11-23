"""The secret scanner: every provider format is caught, placeholders are not, and no match
ever carries the secret it found."""

from __future__ import annotations

import pytest

from msa.secrets import entropy, redact, scan, scan_lines

# Built at runtime so this file itself never contains a scannable credential.
AWS = "AKIA" + "Q3EGX7PLMR2TBN5W"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
ANTHROPIC = "sk-ant-" + "api03-Zx9Qw8Er7Ty6Ui5Op4As3Df2"
SLACK = "xoxb-" + "1234567890-abcdefghij"
GOOGLE = "AIza" + "SyD3x7Q9mZ2pL5kR8tV1nB4cH6jW0eY3uI8"
STRIPE = "sk_live_" + "51HqLyjWDarjtT1zdp7dcXYZ"
KEY_BLOCK = "-----BEGIN " + "RSA PRIVATE KEY-----"
RANDOM = "q8Zr2Lx7Wk4Np9Tv3Hs6"


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        (f'AWS_ACCESS_KEY_ID = "{AWS}"', "aws access key id"),
        (f"token: {GITHUB}", "github token"),
        (f"client = Anthropic(api_key='{ANTHROPIC}')", "anthropic api key"),
        (f"SLACK={SLACK}", "slack token"),
        (f"maps_key = {GOOGLE}", "google api key"),
        (f"stripe.api_key = '{STRIPE}'", "stripe live key"),
        (KEY_BLOCK, "private key"),
        (f'DB_PASSWORD = "{RANDOM}"', "credential assignment"),
        (f'"api_key": "{RANDOM}"', "credential assignment"),
    ],
)
def test_each_kind_of_secret_is_found_once(line: str, rule: str) -> None:
    (match,) = scan(line)
    assert match.rule == rule
    assert match.line == 1


@pytest.mark.parametrize(
    "line",
    [
        'password = "changeme-please-now"',
        'API_KEY = "your-api-key-goes-here"',
        'token = "${GITHUB_TOKEN_FROM_ENV}"',
        'secret = "aaaaaaaaaaaaaaaaaaaa"',  # long but not random
        'password = "correcthorsebattery"',  # words: low entropy
        "token = os.environ['GITHUB_TOKEN']",  # read from the environment: the right way
        'api_key = "test-key-0123456789"',
        "the AKIA prefix names AWS keys",
        "sk-short",
    ],
)
def test_placeholders_and_environment_reads_are_not_secrets(line: str) -> None:
    assert scan(line) == []


def test_matches_never_carry_the_secret() -> None:
    text = "\n".join([f'k = "{AWS}"', f"t = {GITHUB}", f'password = "{RANDOM}"'])
    for match in scan(text):
        rendered = match.render("app.py")
        for secret in (AWS, GITHUB, RANDOM):
            assert secret not in rendered
            assert secret not in match.redacted


def test_redaction_keeps_a_prefix_and_the_length() -> None:
    assert redact(AWS) == f"AKIA… ({len(AWS)} chars)"


def test_lines_keep_their_numbers() -> None:
    found = scan_lines([(10, "ok"), (42, f"key = '{AWS}'")])
    assert [(m.line, m.rule) for m in found] == [(42, "aws access key id")]
    assert (
        found[0].render("src/settings.py")
        == f"src/settings.py:42: aws access key id ({redact(AWS)})"
    )


def test_entropy_separates_keys_from_words() -> None:
    assert entropy("") == 0.0
    assert entropy("aaaa") == 0.0
    assert entropy(RANDOM) > 4.0
    assert entropy("correcthorsebattery") < 3.5

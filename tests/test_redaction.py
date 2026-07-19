from bedolaga_grace_bridge.security.redaction import redact


def test_redacts_secrets_dsn_bearer_and_uuid() -> None:
    secret = "top-secret-value"
    text = """REMNAWAVE_API_KEY=top-secret-value
Authorization: Bearer abc.def.ghi
postgresql://user:password@db/database
user=6ba7b810-9dad-41d1-80b4-00c04fd430c8
"""
    result = redact(text, [secret])
    assert secret not in result
    assert "abc.def.ghi" not in result
    assert ":password@" not in result
    assert "6ba7b810-9dad-41d1-80b4-00c04fd430c8" not in result
    assert "uuid:" in result

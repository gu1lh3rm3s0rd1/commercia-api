from app.core.security import create_access_token, decode_access_token, hash_password, verify_password


def test_password_hash_roundtrip():
    hashed = hash_password("s3cr3t")
    assert hashed != "s3cr3t"
    assert verify_password("s3cr3t", hashed)
    assert not verify_password("wrong-password", hashed)


def test_jwt_roundtrip():
    token = create_access_token(subject="42")
    assert decode_access_token(token) == "42"

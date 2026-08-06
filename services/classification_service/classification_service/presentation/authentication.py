import secrets

from fastapi import HTTPException


def authenticate_bearer(authorization: str | None, expected_token: str) -> None:
    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or not supplied
        or not secrets.compare_digest(supplied, expected_token)
    ):
        raise HTTPException(status_code=401, detail="authentication required")

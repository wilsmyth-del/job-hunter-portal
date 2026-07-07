"""
tokens.py — stable, non-expiring signed tokens shared by app.py (Flask) and
scraper.py (standalone script). Used for the unsubscribe link in digest
emails, which must keep working indefinitely without a stored expiry.
"""

import os

from itsdangerous import URLSafeSerializer

_SALT = "unsubscribe"


def _serializer() -> URLSafeSerializer:
    secret = os.getenv("SECRET_KEY")
    return URLSafeSerializer(secret, salt=_SALT)


def unsubscribe_token(user_id: int) -> str:
    return _serializer().dumps(user_id)


def user_id_from_token(token: str):
    try:
        return _serializer().loads(token)
    except Exception:
        return None

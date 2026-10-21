from cryptography.fernet import Fernet, InvalidToken
from app.core.config import settings
from app.core.logger import logger


class SessionCrypto:
    def __init__(self):
        key = settings.session_encryption_key
        if not key:
            raise ValueError("SESSION_ENCRYPTION_KEY is not set!")
        self.cipher = Fernet(key.encode())

    def encrypt(self, session_string: str) -> str:
        return self.cipher.encrypt(session_string.encode()).decode()

    def decrypt(self, encrypted_session: str) -> str:
        try:
            return self.cipher.decrypt(encrypted_session.encode()).decode()
        except InvalidToken:
            logger.error("Failed to decrypt session: invalid token or wrong key.")
            raise ValueError("Invalid session data")


crypto = SessionCrypto()

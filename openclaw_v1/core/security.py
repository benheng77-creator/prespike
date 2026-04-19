from cryptography.fernet import Fernet
import os


class SecurityVault:
    def __init__(self):
        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            raise RuntimeError(
                "ENCRYPTION_KEY is not set. Generate one with:\n"
                '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
                "then add it to config/.env as ENCRYPTION_KEY=<value>"
            )
        self.cipher = Fernet(key.encode() if isinstance(key, str) else key)

    def decrypt_keys(self, encrypted_string):
        return self.cipher.decrypt(encrypted_string.encode()).decode()

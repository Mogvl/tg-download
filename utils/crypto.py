"""Crypto utils"""

import base64

from Crypto.Cipher import AES


class AesBase64(object):
    """for AES encryption"""

    def __init__(self, key: str, iv: str):
        self.key = key.encode("utf-8")
        self.iv = iv.encode("utf-8")
        self.mode = AES.MODE_CBC

    def encrypt(self, content):
        """
        Encrypts the given content using the AES encryption algorithm.

        Parameters:
            content (str): The content to be encrypted.

        Returns:
            str: The encrypted content encoded in base64.
        """
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        content_padding = self.pkcs7padding(content)
        encrypt_bytes = cipher.encrypt(content_padding)
        return base64.b64encode(encrypt_bytes)

    def decrypt(self, content):
        """
        Decrypts the given content using AES encryption
        with Cipher Block Chaining (CBC) mode.

        Parameters:
            content (str): The content to be decrypted.

        Returns:
            str: The decrypted text, or None on any error (malformed input
            must not raise, otherwise login endpoint 500s).
        """
        try:
            raw = base64.b64decode(content, validate=False)
            if len(raw) == 0 or len(raw) % 16 != 0:
                return None
            cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
            text = cipher.decrypt(raw)
            return self.pkcs7unpadding(text)
        except Exception:
            return None

    def pkcs7unpadding(self, text: bytes) -> str:
        """
        Removes the PKCS#7 padding from the given bytes.

        Parameters:
            text (bytes): The decrypted bytes.

        Returns:
            str: The text without PKCS#7 padding (utf-8 decoded).
        """
        if not text:
            return ""
        pad = text[-1]
        if pad < 1 or pad > 16 or pad > len(text):
            # 非法 padding，按无 padding 处理
            return text.decode("utf-8", errors="replace")
        return text[:-pad].decode("utf-8", errors="replace")

    def pkcs7padding(self, text: str) -> bytes:
        """
        Adds PKCS7 padding to the given text, strictly by UTF-8 bytes.

        Args:
            text (str): The text to be padded.

        Returns:
            bytes: The padded UTF-8 bytes.
        """
        bs = 16
        data = text.encode("utf-8")
        padding = bs - len(data) % bs
        return data + bytes([padding]) * padding

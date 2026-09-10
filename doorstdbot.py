from __future__ import annotations
"""Standalone executable TD Bot-as-a-Service application.

This file intentionally contains the full runtime assembly so deploying
`python doorstdbot.py` does not require a launcher indirection.
"""

# ===== td_platform/config.py =====

import os
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True, slots=True)
class Settings:
    root_bot_token: str
    root_admin_id: int
    token_encryption_key: str
    data_dir: Path
    log_level: str = "INFO"
    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("ROOT_BOT_TOKEN", "").strip()
        admin = os.getenv("ROOT_ADMIN_ID", "").strip()
        key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
        if not token:
            raise RuntimeError("ROOT_BOT_TOKEN is required")
        if not admin.isdigit():
            raise RuntimeError("ROOT_ADMIN_ID must be numeric")
        if not key:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY is required")
        data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(token, int(admin), key, data_dir, os.getenv("LOG_LEVEL", "INFO").upper())

# ===== remaining platform modules =====

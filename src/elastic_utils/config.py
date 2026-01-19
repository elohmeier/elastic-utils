"""Configuration and credential storage management."""

import json
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from platformdirs import user_data_dir

APP_NAME = "elastic-utils"


class Credentials(TypedDict):
    url: str
    api_key_id: str
    api_key: str
    created_at: str


def get_data_dir() -> Path:
    """Get the XDG data directory for elastic-utils."""
    return Path(user_data_dir(APP_NAME))


def get_credentials_path() -> Path:
    """Get the path to the credentials file."""
    return get_data_dir() / "credentials.json"


def save_credentials(url: str, api_key_id: str, api_key: str) -> Path:
    """Save credentials to the data directory."""
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    credentials: Credentials = {
        "url": url,
        "api_key_id": api_key_id,
        "api_key": api_key,
        "created_at": datetime.now().isoformat(),
    }

    creds_path = get_credentials_path()
    creds_path.write_text(json.dumps(credentials, indent=2))
    creds_path.chmod(0o600)  # Restrict permissions to owner only
    return creds_path


def load_credentials() -> Credentials | None:
    """Load credentials from the data directory."""
    creds_path = get_credentials_path()
    if not creds_path.exists():
        return None
    return json.loads(creds_path.read_text())


def delete_credentials() -> bool:
    """Delete the credentials file. Returns True if file existed."""
    creds_path = get_credentials_path()
    if creds_path.exists():
        creds_path.unlink()
        return True
    return False

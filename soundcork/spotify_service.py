import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from soundcork.config import Settings

logger = logging.getLogger(__name__)

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyService:
    def __init__(self, settings: Settings | None = None):
        self._settings = settings or Settings()
        self._accounts_file = os.path.join(
            self._settings.data_dir, "spotify", "accounts.json"
        )

    def _ensure_spotify_dir(self):
        spotify_dir = os.path.dirname(self._accounts_file)
        os.makedirs(spotify_dir, exist_ok=True)

    def _load_accounts(self) -> list[dict]:
        if not os.path.isfile(self._accounts_file):
            return []
        try:
            with open(self._accounts_file, "r", encoding="utf-8") as file:
                return json.load(file)
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read Spotify accounts file")
            return []

    def _save_accounts(self, accounts: list[dict]):
        self._ensure_spotify_dir()
        with open(self._accounts_file, "w", encoding="utf-8") as file:
            json.dump(accounts, file, indent=2)

    def _refresh_access_token(self, refresh_token: str) -> dict | None:
        credentials = (
            f"{self._settings.spotify_client_id}:{self._settings.spotify_client_secret}"
        )
        basic_auth = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            SPOTIFY_TOKEN_URL,
            data=body,
            headers={
                "Authorization": f"Basic {basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        try:
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError):
            logger.exception("Spotify token refresh failed")
            return None

    def get_fresh_token_sync(self) -> str | None:
        accounts = self._load_accounts()
        if not accounts:
            return None

        if not (
            self._settings.spotify_client_id and self._settings.spotify_client_secret
        ):
            return None

        account = accounts[0]
        now = int(time.time())
        if now >= int(account.get("tokenExpiresAt", 0)) - 60:
            refresh_token = account.get("refreshToken", "")
            if not refresh_token:
                logger.warning("No Spotify refresh token available")
                return None

            token_data = self._refresh_access_token(refresh_token)
            if not token_data or "access_token" not in token_data:
                return None

            account["accessToken"] = token_data["access_token"]
            account["tokenExpiresAt"] = now + int(token_data.get("expires_in", 3600))
            if "refresh_token" in token_data:
                account["refreshToken"] = token_data["refresh_token"]
            self._save_accounts(accounts)

        return account.get("accessToken")

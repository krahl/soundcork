import base64
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import soundcork.main as main
from soundcork.marge import configured_source_xml
from soundcork.model import ConfiguredSource
from soundcork.spotify_service import SpotifyTokenRefreshError

ACCOUNT_ID = "1234567"
DEVICE_ID = "ABCDEF012345"
TUNEIN_SERIAL = "00000000-0000-4000-8000-000000000001"
EXAMPLE_SPOTIFY_DISPLAY_NAME = "Example Spotify Account"
EXAMPLE_SPOTIFY_USERNAME = "example_spotify_user"
EXAMPLE_SPOTIFY_SECRET = "example-spotify-secret"
TUNEIN_TOKEN = base64.urlsafe_b64encode(
    f'{{"serial":"{TUNEIN_SERIAL}"}}'.encode("utf-8")
).decode("ascii")


def _write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_datastore(root: Path):
    account_dir = root / ACCOUNT_ID
    devices_dir = account_dir / "devices" / DEVICE_ID

    _write_text(
        devices_dir / "DeviceInfo.xml",
        f"""
<info deviceID="{DEVICE_ID}">
  <name>Example Speaker</name>
  <type>SoundTouch Portable</type>
  <moduleType>scm</moduleType>
  <components>
    <component>
      <componentCategory>SCM</componentCategory>
      <softwareVersion>27.0.8</softwareVersion>
      <serialNumber>EXAMPLE-SCM-SERIAL</serialNumber>
    </component>
    <component>
      <componentCategory>PackagedProduct</componentCategory>
      <serialNumber>EXAMPLE-PRODUCT-SERIAL</serialNumber>
    </component>
  </components>
  <networkInfo type="SCM">
    <ipAddress>192.0.2.81</ipAddress>
  </networkInfo>
  <createdOn>2016-11-05T20:25:47.000+00:00</createdOn>
  <updatedOn>2023-04-30T17:37:19.000+00:00</updatedOn>
</info>
""".strip(),
    )

    _write_text(
        account_dir / "Presets.xml",
        """
<presets>
  <preset id="1" createdOn="1694260000" updatedOn="1694260000">
    <ContentItem source="TUNEIN" type="stationurl" location="/v1/playback/station/s25260" isPresetable="true">
      <itemName>1LIVE</itemName>
      <containerArt>http://cdn-profiles.tunein.com/s25260/images/logog.jpg</containerArt>
    </ContentItem>
  </preset>
</presets>
""".strip(),
    )

    _write_text(account_dir / "Recents.xml", "<recents />")

    _write_text(
        account_dir / "Sources.xml",
        f"""
<sources>
  <source id="14599013" displayName="{EXAMPLE_SPOTIFY_DISPLAY_NAME}" secret="{EXAMPLE_SPOTIFY_SECRET}" secretType="token_version_3">
    <createdOn>2017-07-08T10:26:18.000+00:00</createdOn>
    <updatedOn>2019-02-10T16:18:26.000+00:00</updatedOn>
    <sourceKey account="{EXAMPLE_SPOTIFY_USERNAME}" type="SPOTIFY" />
  </source>
  <source id="14688827" displayName="" secret="{TUNEIN_TOKEN}" secretType="token">
    <createdOn>2017-07-19T17:24:42.000+00:00</createdOn>
    <updatedOn>2020-03-28T12:39:05.000+00:00</updatedOn>
    <sourceKey account="" type="TUNEIN" />
  </source>
</sources>
""".strip(),
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    _seed_datastore(tmp_path)
    monkeypatch.setattr(main.datastore, "data_dir", str(tmp_path))
    monkeypatch.setattr(main.settings, "base_url", "http://soundcork.example.com")
    monkeypatch.setattr(main.settings, "extended_bmx_registry", False)
    return TestClient(main.app)


def test_account_devices_matches_stockholm_shape(client: TestClient):
    response = client.get(f"/marge/streaming/account/{ACCOUNT_ID}/devices")

    assert response.status_code == 200
    assert (
        response.headers["content-type"]
        == "application/vnd.bose.streaming-v1.1+xml"
    )
    assert response.headers["Method_name"] == "getDevices"
    assert "<devices>" in response.text
    assert f'<device deviceid="{DEVICE_ID}">' in response.text
    assert "<providerSettings>" in response.text


def test_account_presets_all_matches_stockholm_shape(client: TestClient):
    response = client.get(f"/marge/streaming/account/{ACCOUNT_ID}/presets/all")

    assert response.status_code == 200
    assert (
        response.headers["content-type"]
        == "application/vnd.bose.streaming-v1.1+xml"
    )
    assert "<presets>" in response.text
    assert 'buttonNumber="1"' in response.text
    assert "/v1/playback/station/s25260" in response.text
    assert "<sourceproviderid>25</sourceproviderid>" in response.text


def test_music_provider_eligibility_defaults_false(client: TestClient):
    response = client.post(
        "/marge/streaming/music/musicprovider/26/is_eligible",
        content='<?xml version="1.0"?><account><accountId></accountId></account>',
        headers={"Content-Type": "application/vnd.bose.streaming-v1.1+xml"},
    )

    assert response.status_code == 200
    assert (
        response.headers["content-type"]
        == "application/vnd.bose.streaming-v1.1+xml"
    )
    assert "<isEligible>false</isEligible>" in response.text


def test_configured_source_xml_preserves_secret_type():
    source = ConfiguredSource(
        display_name="Spotify",
        id="14599013",
        secret=EXAMPLE_SPOTIFY_SECRET,
        secret_type="token_version_3",
        source_key_type="SPOTIFY",
        source_key_account=EXAMPLE_SPOTIFY_USERNAME,
        created_on="2017-07-08T10:26:18.000+00:00",
        updated_on="2019-02-10T16:18:26.000+00:00",
    )

    xml = configured_source_xml(source)
    credential = xml.find("credential")

    assert credential is not None
    assert credential.attrib["type"] == "token_version_3"


def test_bmx_registry_defaults_to_stockholm_subset(client: TestClient):
    response = client.get("/bmx/registry/v1/services")

    assert response.status_code == 200
    service_names = [service["id"]["name"] for service in response.json()["bmx_services"]]
    assert service_names == ["TUNEIN", "LOCAL_INTERNET_RADIO", "SIRIUSXM_EVEREST"]


def test_bmx_tunein_token_echoes_refresh_token(client: TestClient):
    response = client.post(
        "/bmx/tunein/v1/token",
        json={"grant_type": "refresh_token", "refresh_token": TUNEIN_TOKEN},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["access_token"] == TUNEIN_TOKEN
    assert payload["refresh_token"] == TUNEIN_TOKEN


def test_spotify_oauth_token_refresh_uses_posted_refresh_token(
    client: TestClient, monkeypatch
):
    monkeypatch.setattr(main.settings, "spotify_client_id", "client-id")
    monkeypatch.setattr(main.settings, "spotify_client_secret", "client-secret")

    refresh_token = "example-spotify-refresh-token"
    captured: dict[str, str] = {}

    def fake_get_fresh_token_sync(refresh_token: str = "") -> str | None:
        captured["refresh_token"] = refresh_token
        return "example-spotify-access-token"

    monkeypatch.setattr(main.spotify_service, "get_fresh_token_sync", fake_get_fresh_token_sync)

    response = client.post(
        f"/marge/oauth/account/{ACCOUNT_ID}/music/musicprovider/15/token/cs3",
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "code": "",
            "redirect_uri": "",
        },
    )

    assert response.status_code == 200
    assert captured["refresh_token"] == refresh_token
    assert response.json()["access_token"] == "example-spotify-access-token"
    assert response.json()["token_type"] == "Bearer"


def test_spotify_oauth_token_refresh_surfaces_spotify_error(
    client: TestClient, monkeypatch
):
    monkeypatch.setattr(main.settings, "spotify_client_id", "client-id")
    monkeypatch.setattr(main.settings, "spotify_client_secret", "client-secret")

    def fake_get_fresh_token_sync(refresh_token: str = "") -> str | None:
        raise SpotifyTokenRefreshError(
            400,
            {
                "error": "invalid_grant",
                "error_description": "Invalid refresh token",
            },
        )

    monkeypatch.setattr(main.spotify_service, "get_fresh_token_sync", fake_get_fresh_token_sync)

    response = client.post(
        f"/marge/oauth/account/{ACCOUNT_ID}/music/musicprovider/15/token/cs3",
        json={
            "grant_type": "refresh_token",
            "refresh_token": "example-spotify-refresh-token",
            "code": "",
            "redirect_uri": "",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_grant",
        "error_description": "Invalid refresh token",
    }


def test_bmx_tunein_navigate_allows_anonymous_access(
    client: TestClient, monkeypatch
):
    def fake_fetch(url: str):
        return {
            "head": {"title": "Local Radio"},
            "body": [
                {
                    "text": "Preview",
                    "children": [
                        {
                            "text": "WDR 2 Rheinland",
                            "guide_id": "s213886",
                            "subtext": "NRW",
                            "image": "http://cdn-radiotime-logos.tunein.com/s213886g.png",
                        }
                    ],
                }
            ],
        }

    monkeypatch.setattr("soundcork.bmx.fetch_tunein_json", fake_fetch)

    response = client.get("/bmx/tunein/v1/navigate")

    assert response.status_code == 200
    payload = response.json()
    assert payload["_links"]["bmx_search"]["href"] == "/v1/search?q={query}"
    section_names = [section["name"] for section in payload["bmx_sections"]]
    assert "Saved SoundTouch presets" not in section_names
    assert "Local Radio" in section_names


def test_bmx_tunein_navigate_returns_sections_when_authorized(
    client: TestClient, monkeypatch
):
    def fake_fetch(url: str):
        return {
            "head": {"title": "Local Radio"},
            "body": [
                {
                    "text": "Preview",
                    "children": [
                        {
                            "text": "WDR 2 Rheinland",
                            "guide_id": "s213886",
                            "subtext": "NRW",
                            "image": "http://cdn-radiotime-logos.tunein.com/s213886g.png",
                        },
                        {
                            "text": "NDR Info",
                            "guide_id": "s24885",
                            "subtext": "News",
                            "image": "http://cdn-profiles.tunein.com/s24885/images/logog.png",
                        },
                    ],
                }
            ],
        }

    monkeypatch.setattr("soundcork.bmx.fetch_tunein_json", fake_fetch)

    response = client.get(
        "/bmx/tunein/v1/navigate",
        headers={"Authorization": TUNEIN_TOKEN},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["_links"]["bmx_search"]["href"] == "/v1/search?q={query}"
    section_names = [section["name"] for section in payload["bmx_sections"]]
    assert "Saved SoundTouch presets" in section_names
    assert "Local Radio" in section_names

    playback_links = [
        item["_links"]["bmx_playback"]["href"]
        for section in payload["bmx_sections"]
        for item in section.get("items", [])
        if "_links" in item and "bmx_playback" in item["_links"]
    ]
    assert "/v1/playback/station/s25260" in playback_links
    assert "/v1/playback/station/s213886" in playback_links


def test_bmx_tunein_navigate_normalizes_plain_browse_urls(
    client: TestClient, monkeypatch
):
    raw_url = (
        "http://opml.radiotime.com/Browse.ashx?"
        f"id=g22&formats=mp3,aac,ogg&serial={TUNEIN_SERIAL}"
    )
    captured: dict[str, str] = {}

    def fake_fetch(url: str):
        captured["url"] = url
        return {
            "head": {"title": "Genres"},
            "body": [
                {
                    "text": "Music",
                    "URL": "http://opml.radiotime.com/Browse.ashx?id=g23",
                }
            ],
        }

    monkeypatch.setattr("soundcork.bmx.fetch_tunein_json", fake_fetch)

    encoded_target = base64.urlsafe_b64encode(raw_url.encode("utf-8")).decode("ascii")
    response = client.get(
        f"/bmx/tunein/v1/navigate/{encoded_target}",
        headers={"Authorization": TUNEIN_TOKEN},
    )

    assert response.status_code == 200
    parsed_target = urllib.parse.urlparse(captured["url"])
    assert parsed_target.scheme == "https"
    assert parsed_target.netloc == "opml.radiotime.com"
    assert urllib.parse.parse_qs(parsed_target.query)["id"] == ["g22"]
    assert urllib.parse.parse_qs(parsed_target.query)["render"] == ["json"]

    next_href = response.json()["bmx_sections"][0]["items"][0]["_links"]["bmx_navigate"]["href"]
    decoded_next_href = base64.urlsafe_b64decode(next_href.rsplit("/", 1)[-1]).decode(
        "utf-8"
    )
    parsed_next = urllib.parse.urlparse(decoded_next_href)
    assert parsed_next.scheme == "https"
    assert parsed_next.netloc == "opml.radiotime.com"
    assert urllib.parse.parse_qs(parsed_next.query)["id"] == ["g23"]
    assert urllib.parse.parse_qs(parsed_next.query)["render"] == ["json"]

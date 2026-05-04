import base64
import json
import urllib.parse

from soundcork.bmx import (
    _build_tunein_browse_url,
    tunein_navigate_v1,
    tunein_navigation,
    tunein_root_navigation,
    tunein_search_v1,
)


class FakeTuneInResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def encode_uri(uri: str) -> str:
    return base64.urlsafe_b64encode(uri.encode()).decode()


def decode_navigate_href(href: str) -> str:
    return base64.urlsafe_b64decode(href.rsplit("/", 1)[-1]).decode()


def test_navigate_uses_ashx_parser_for_opml_browse_urls(monkeypatch):
    tunein_uri = "http://opml.radiotime.com/Browse.ashx?c=podcast&render=json"
    requested_urls = []

    def fake_urlopen(url):
        requested_urls.append(url)
        return FakeTuneInResponse(
            {
                "head": {"title": "Podcasts"},
                "body": [
                    {
                        "type": "link",
                        "text": "News",
                        "subtext": "Latest episodes",
                        "image": "http://example.com/news.png",
                        "URL": "http://opml.radiotime.com/Browse.ashx?c=news",
                    }
                ],
            }
        )

    monkeypatch.setattr("soundcork.bmx.urllib.request.urlopen", fake_urlopen)

    response = tunein_navigate_v1(encode_uri(tunein_uri))

    assert requested_urls == [tunein_uri]
    assert response.bmx_sections[0].name == "Podcasts"
    assert response.bmx_sections[0].items[0].name == "News"

    navigate_href = response.bmx_sections[0].items[0].links.bmx_navigate.href
    assert (
        decode_navigate_href(navigate_href)
        == "http://opml.radiotime.com/Browse.ashx?c=news&render=json"
    )


def test_search_url_encodes_spaces_and_more_link_uses_encoded_query(monkeypatch):
    requested_urls = []

    def fake_urlopen(url):
        requested_urls.append(url)
        return FakeTuneInResponse(
            {
                "Items": [
                    {
                        "Type": "Container",
                        "ContainerType": "PlayableStations",
                        "Title": "Stations",
                        "Children": [
                            {
                                "Type": "Station",
                                "GuideId": "s12345",
                                "Title": "Radio Paradise",
                                "Subtitle": "Commercial free",
                                "Image": "http://example.com/radio-paradise.png",
                            }
                        ],
                    }
                ]
            }
        )

    monkeypatch.setattr("soundcork.bmx.urllib.request.urlopen", fake_urlopen)

    response = tunein_search_v1("radio paradise")

    assert " " not in requested_urls[0]
    requested_query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(requested_urls[0]).query
    )
    assert requested_query["query"] == ["radio paradise"]

    section_href = response.bmx_sections[0].links.self.href
    decoded_section_uri = decode_navigate_href(section_href)
    assert " " not in decoded_section_uri
    decoded_query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(decoded_section_uri).query
    )
    assert decoded_query["query"] == ["radio paradise"]
    assert response.bmx_sections[0].items[0].links.bmx_playback.href == (
        "/v1/playback/station/s12345"
    )


def test_root_navigation_uses_local_categories_without_bose_image_links(monkeypatch):
    def fake_fetch_tunein_json(url):
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

    monkeypatch.setattr("soundcork.bmx.fetch_tunein_json", fake_fetch_tunein_json)

    response = tunein_root_navigation()

    assert response.links.bmx_search.href == "/v1/search?q={query}"
    assert any(section.name == "Local Radio" for section in response.bmx_sections)

    image_urls = [
        item.image_url
        for section in response.bmx_sections
        for item in section.items
        if item.image_url
    ]
    assert all("bose" not in image_url for image_url in image_urls)


def test_tunein_browse_url_accepts_configured_region_id():
    url = _build_tunein_browse_url(
        "speaker-serial",
        "local",
        "id=r100447",
    )
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)

    assert query["id"] == ["r100447"]
    assert "c" not in query
    assert query["serial"] == ["speaker-serial"]
    assert query["render"] == ["json"]


def test_tunein_browse_url_accepts_configured_latlon_shorthand():
    url = _build_tunein_browse_url(
        "",
        "trending",
        "51.23,6.77",
    )
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)

    assert query["latlon"] == ["51.23,6.77"]
    assert query["c"] == ["trending"]
    assert query["render"] == ["json"]


def test_navigation_rejects_non_tunein_targets():
    encoded_target = encode_uri(
        "https://media.bose.io/bmx-icons/tunein/top-menu/news.png"
    )

    try:
        tunein_navigation(encoded_target)
    except ValueError as exc:
        assert str(exc) == "Unsupported TuneIn navigation target"
    else:
        raise AssertionError("TuneIn navigation accepted a non-TuneIn URL")

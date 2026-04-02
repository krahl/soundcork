import base64
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from soundcork.model import (
    Audio,
    BmxPlaybackResponse,
    BmxPodcastInfoResponse,
    Stream,
    Track,
)
from soundcork.utils import strip_element_text

# TODO: move into constants file eventually.
TUNEIN_DESCRIBE = "https://opml.radiotime.com/describe.ashx?id=%s"
TUNEIN_STREAM = "http://opml.radiotime.com/Tune.ashx?id=%s&formats=mp3,aac,ogg"
TUNEIN_BROWSE = (
    "https://opml.radiotime.com/Browse.ashx?render=json&formats=mp3,aac,ogg&%s"
)
TUNEIN_SEARCH = (
    "https://opml.radiotime.com/Search.ashx?render=json&formats=mp3,aac,ogg&query=%s"
)
TUNEIN_ALLOWED_HOSTS = {"opml.radiotime.com", "api.radiotime.com"}
TUNEIN_ROOT_CATEGORIES = [
    ("Local Radio", "local"),
    ("Trending", "trending"),
    ("Music", "music"),
]
TUNEIN_MENU_CATEGORIES = [
    ("Local Radio", "local", "https://media.bose.io/bmx-icons/tunein/top-menu/location.png"),
    ("Podcasts", "talk", "https://media.bose.io/bmx-icons/tunein/top-menu/podcasts.png"),
    ("Music", "music", "https://media.bose.io/bmx-icons/tunein/top-menu/note.png"),
    ("Sports", "sports", "https://media.bose.io/bmx-icons/tunein/top-menu/trophy.png"),
    ("News & Talk", "news", "https://media.bose.io/bmx-icons/tunein/top-menu/news.png"),
    ("Languages", "language", "https://media.bose.io/bmx-icons/tunein/top-menu/bubble.png"),
]


def fetch_tunein_json(url: str) -> dict:
    contents = urllib.request.urlopen(url).read()
    return json.loads(contents.decode("utf-8"))


def tunein_token(refresh_token: str) -> dict:
    return {
        "access_token": refresh_token,
        "refresh_token": refresh_token,
    }


def tunein_root_navigation(
    auth_token: str,
    favorites_items: list[dict] | None = None,
) -> dict:
    serial = tunein_serial_from_token(auth_token)
    sections = []
    favorites_items = favorites_items or []
    if favorites_items:
        sections.append(
            {
                "_links": {"self": {"href": _navigation_href("soundcork://favorites")}},
                "items": favorites_items[:6],
                "layout": "ribbon",
                "name": "Saved SoundTouch presets",
            }
        )

    for label, category in TUNEIN_ROOT_CATEGORIES:
        preview = _preview_category_section(label, category, serial)
        if preview:
            sections.append(preview)

    sections.append(
        {
            "_links": {"self": {"href": "/v1/navigate/"}},
            "items": [
                {
                    "_links": {
                        "bmx_navigate": {
                            "href": _navigation_href(
                                _build_tunein_browse_url(serial, category)
                            )
                        }
                    },
                    "imageUrl": icon,
                    "name": label,
                    "subtitle": "",
                }
                for label, category, icon in TUNEIN_MENU_CATEGORIES
            ],
            "name": "",
        }
    )

    return {
        "_links": {
            "bmx_search": {
                "filters": [],
                "href": "/v1/search?q={query}",
                "templated": True,
            },
            "self": {"href": "/v1/navigate"},
        },
        "bmx_sections": sections,
        "layout": "classic",
    }


def tunein_navigation(
    encoded_target: str,
    auth_token: str,
    favorites_items: list[dict] | None = None,
) -> dict:
    target = _normalize_tunein_navigation_url(_decode_navigation_target(encoded_target))
    if target == "soundcork://favorites":
        return {
            "_links": {"self": {"href": _navigation_href(target)}},
            "bmx_sections": [
                {
                    "_links": {"self": {"href": _navigation_href(target)}},
                    "items": favorites_items or [],
                    "layout": "ribbon",
                    "name": "Saved SoundTouch presets",
                }
            ],
            "layout": "classic",
        }

    parsed_target = urllib.parse.urlparse(target)
    if parsed_target.hostname not in TUNEIN_ALLOWED_HOSTS:
        raise ValueError("Unsupported TuneIn navigation target")

    payload = fetch_tunein_json(target)
    return _payload_to_navigation(payload, target)


def tunein_search_navigation(query: str, auth_token: str) -> dict:
    payload = fetch_tunein_json(TUNEIN_SEARCH % urllib.parse.quote_plus(query))
    return _payload_to_navigation(payload, None, f"/v1/search?q={query}")


def tunein_serial_from_token(auth_token: str) -> str:
    token = auth_token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    padded = token + "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        data = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return ""

    if isinstance(data, dict):
        return str(data.get("serial", ""))
    return ""


def _preview_category_section(label: str, category: str, serial: str) -> dict | None:
    category_url = _build_tunein_browse_url(serial, category)
    try:
        payload = fetch_tunein_json(category_url)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return None

    entries = _payload_entries(payload)
    items = _entries_to_items(entries, limit=6)
    if not items:
        for entry in entries:
            items = _entries_to_items(_entry_children(entry), limit=6)
            if items:
                break
    if not items:
        return None

    return {
        "_links": {"self": {"href": _navigation_href(category_url)}},
        "items": items,
        "layout": "ribbon",
        "name": label,
    }


def _build_tunein_browse_url(serial: str, category: str) -> str:
    query = {"c": category}
    if serial:
        query["serial"] = serial
    return TUNEIN_BROWSE % urllib.parse.urlencode(query)


def _decode_navigation_target(encoded_target: str) -> str:
    padded = encoded_target + "=" * (-len(encoded_target) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")


def _navigation_href(target: str) -> str:
    encoded = base64.urlsafe_b64encode(target.encode("utf-8")).decode("ascii")
    return f"/v1/navigate/{encoded}"


def _normalize_tunein_navigation_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in TUNEIN_ALLOWED_HOSTS:
        return url

    path = parsed.path.lower()
    if not (path.endswith("/browse.ashx") or path.endswith("/search.ashx")):
        return url

    query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query_items = [(key, value) for key, value in query_items if key.lower() != "render"]
    query_items.append(("render", "json"))

    return urllib.parse.urlunparse(
        parsed._replace(
            scheme="https",
            query=urllib.parse.urlencode(query_items),
        )
    )


def _payload_to_navigation(
    payload: dict, source_url: str | None, self_href: str | None = None
) -> dict:
    entries = _payload_entries(payload)
    title = payload.get("head", {}).get("title", "") if isinstance(payload, dict) else ""
    sections = []
    ungrouped_items = []

    for entry in entries:
        child_items = _entries_to_items(_entry_children(entry), limit=12)
        if child_items:
            section_name = _entry_name(entry) or title
            section_url = _entry_url(entry) or source_url
            section = {
                "items": child_items,
                "name": section_name,
            }
            if section_url:
                section["_links"] = {"self": {"href": _navigation_href(section_url)}}
            if len(child_items) > 1:
                section["layout"] = "ribbon"
            sections.append(section)
            continue

        item = _entry_to_item(entry)
        if item:
            ungrouped_items.append(item)

    if ungrouped_items:
        section = {
            "items": ungrouped_items,
            "name": title,
        }
        if source_url:
            section["_links"] = {"self": {"href": _navigation_href(source_url)}}
        sections.insert(0, section)

    return {
        "_links": {"self": {"href": self_href or _navigation_href(source_url or "")}},
        "bmx_sections": sections,
        "layout": "classic",
    }


def _payload_entries(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []

    body = payload.get("body", [])
    if isinstance(body, list):
        return [entry for entry in body if isinstance(entry, dict)]
    if isinstance(body, dict):
        if isinstance(body.get("children"), list):
            return [entry for entry in body["children"] if isinstance(entry, dict)]
        if isinstance(body.get("items"), list):
            return [entry for entry in body["items"] if isinstance(entry, dict)]
    if isinstance(payload.get("children"), list):
        return [entry for entry in payload["children"] if isinstance(entry, dict)]
    if isinstance(payload.get("items"), list):
        return [entry for entry in payload["items"] if isinstance(entry, dict)]
    return []


def _entry_children(entry: dict) -> list[dict]:
    children = entry.get("children") or entry.get("items") or []
    if isinstance(children, list):
        return [child for child in children if isinstance(child, dict)]
    return []


def _entries_to_items(entries: list[dict], limit: int | None = None) -> list[dict]:
    items = []
    for entry in entries:
        item = _entry_to_item(entry)
        if item:
            items.append(item)
        if limit is not None and len(items) >= limit:
            break
    return items


def _entry_to_item(entry: dict) -> dict | None:
    station_id = _station_id(entry)
    item_name = _entry_name(entry)
    subtitle = _entry_subtitle(entry)
    image_url = _entry_image(entry)

    if station_id:
        playback_href = f"/v1/playback/station/{station_id}"
        return {
            "_links": {
                "bmx_playback": {"href": playback_href, "type": "stationurl"},
                "bmx_preset": {
                    "containerArt": image_url,
                    "href": playback_href,
                    "name": item_name,
                    "type": "stationurl",
                },
            },
            "imageUrl": image_url,
            "name": item_name,
            "subtitle": subtitle,
        }

    entry_url = _entry_url(entry)
    if entry_url:
        return {
            "_links": {"bmx_navigate": {"href": _navigation_href(entry_url)}},
            "imageUrl": image_url,
            "name": item_name,
            "subtitle": subtitle,
        }

    return None


def _station_id(entry: dict) -> str:
    for key in ("guide_id", "guideId", "station_id", "stationId"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith("s"):
            return value

    entry_url = _entry_url(entry)
    if not entry_url:
        return ""

    parsed = urllib.parse.urlparse(entry_url)
    query_params = urllib.parse.parse_qs(parsed.query)
    for value in query_params.get("id", []):
        if value.startswith("s"):
            return value
    return ""


def _entry_name(entry: dict) -> str:
    for key in ("text", "name", "title"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return ""


def _entry_subtitle(entry: dict) -> str:
    for key in ("subtext", "subtitle", "current_song", "description"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return ""


def _entry_image(entry: dict) -> str:
    for key in ("image", "imageUrl", "logo"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return ""


def _entry_url(entry: dict) -> str:
    for key in ("URL", "url", "href"):
        value = entry.get(key)
        if isinstance(value, str):
            return _normalize_tunein_navigation_url(value)
    return ""


# TODO:  determine how listen_id is used, if at all
# TODO:  determine how stream_id is used, if at all
# TODO:  see if there is a value to varying the timeout values
def tunein_playback(station_id: str) -> BmxPlaybackResponse:
    describe_url = TUNEIN_DESCRIBE % station_id
    contents = urllib.request.urlopen(describe_url).read()
    content_str = contents.decode("utf-8")

    root = ET.fromstring(content_str)

    try:
        body = root.find("body")
        outline = body.find("outline")  # type: ignore
        station_elem = outline.find("station")  # type: ignore
    except Exception as e:
        # TODO narrow this exception
        outline = None
        station_elem = None

    name = strip_element_text(station_elem.find("name")) if station_elem else ""
    logo = strip_element_text(station_elem.find("logo")) if station_elem else ""

    # not using these now but leaving the code in for use later
    # current_song_elem = station_elem.find("current_song")
    # current_song = current_song_elem.text if current_song_elem != None else ""
    # current_artist_elem = station_elem.find("current_artist")
    # current_artist = current_artist_elem.text if current_artist_elem != None else ""

    streamreq = TUNEIN_STREAM % station_id
    stream_url_resp = urllib.request.urlopen(streamreq).read().decode("utf-8")

    # these might be used by later calls to bmx_reporting and/or now-playing,
    # so we might need to give them actual values
    stream_id = "e3342"
    listen_id = str(3432432423)
    bmx_reporting_qs = urllib.parse.urlencode(
        {
            "stream_id": stream_id,
            "guide_id": station_id,
            "listen_id": listen_id,
            "stream_type": "liveRadio",
        }
    )
    bmx_reporting = "/v1/report?" + bmx_reporting_qs

    stream_url_list = stream_url_resp.splitlines()
    stream_list = [
        Stream(
            links={"bmx_reporting": {"href": bmx_reporting}},
            hasPlaylist=True,
            isRealtime=True,
            maxTimeout=60,
            bufferingTimeout=20,
            connectingTimeout=10,
            streamUrl=stream_url,
        )
        for stream_url in stream_url_list
    ]

    audio = Audio(
        hasPlaylist=True,
        isRealtime=True,
        maxTimeout=60,
        streamUrl=stream_url_list[0],
        streams=stream_list,
    )
    resp = BmxPlaybackResponse(
        links={
            "bmx_favorite": {"href": "/v1/favorite/" + station_id},
            "bmx_nowplaying": {
                "href": "/v1/now-playing/station/" + station_id,
                "useInternalClient": "ALWAYS",
            },
            "bmx_reporting": {"href": bmx_reporting},
        },
        audio=audio,
        imageUrl=logo,
        isFavorite=False,
        name=name,
        streamType="liveRadio",
    )
    return resp


def tunein_podcast_info(podcast_id: str, encoded_name: str) -> BmxPodcastInfoResponse:

    name = str(base64.urlsafe_b64decode(encoded_name), "utf-8")
    track = Track(
        links={"bmx_track": {"href": f"/v1/playback/episode/{podcast_id}"}},
        is_selected=False,
        name=name,
    )
    resp = BmxPodcastInfoResponse(
        links={
            "self": {
                "href": f"/v1/playback/episodes/{podcast_id}?encoded_name={encoded_name}"
            },
        },
        name=name,
        shuffle_disabled=True,
        repeat_disabled=True,
        stream_type="onDemand",
        tracks=[track],
    )
    return resp


# TODO:  determine how listen_id is used, if at all
# TODO:  determine how stream_id is used, if at all
# TODO:  see if there is a value to varying the timeout values
def tunein_playback_podcast(podcast_id: str) -> BmxPlaybackResponse:

    describe_url = TUNEIN_DESCRIBE % podcast_id
    contents = urllib.request.urlopen(describe_url).read()
    content_str = contents.decode("utf-8")

    root = ET.fromstring(content_str)

    try:
        body = root.find("body")
        outline = body.find("outline")  # type: ignore
        topic = outline.find("topic")  # type: ignore
    except Exception as e:
        # TODO narrow this exception
        outline = None
        topic = None
    title = strip_element_text(topic.find("title")) if topic else ""
    show_title = strip_element_text(topic.find("show_title")) if topic else ""
    duration = strip_element_text(topic.find("duration")) if topic else ""
    show_id = strip_element_text(topic.find("show_id")) if topic else ""
    logo = strip_element_text(topic.find("logo")) if topic else ""

    streamreq = TUNEIN_STREAM % podcast_id
    stream_url_resp = urllib.request.urlopen(streamreq).read().decode("utf-8")

    # these might be used by later calls to bmx_reporting and/or now-playing,
    # so we might need to give them actual values
    stream_id = "e3342"
    listen_id = str(3432432423)
    bmx_reporting_qs = urllib.parse.urlencode(
        {
            "stream_id": stream_id,
            "guide_id": podcast_id,
            "listen_id": listen_id,
            "stream_type": "onDemand",
        }
    )
    bmx_reporting = "/v1/report?" + bmx_reporting_qs

    stream_url_list = stream_url_resp.splitlines()
    stream_list = [
        Stream(
            links={"bmx_reporting": {"href": bmx_reporting}},
            hasPlaylist=True,
            isRealtime=False,
            maxTimeout=60,
            bufferingTimeout=20,
            connectingTimeout=10,
            streamUrl=stream_url,
        )
        for stream_url in stream_url_list
    ]

    audio = Audio(
        hasPlaylist=True,
        isRealtime=False,
        maxTimeout=60,
        streamUrl=stream_url_list[0],
        streams=stream_list,
    )
    resp = BmxPlaybackResponse(
        links={
            "bmx_favorite": {"href": f"/v1/favorite/{show_id}"},
            "bmx_reporting": {"href": bmx_reporting},
        },
        artist={"name": show_title},
        audio=audio,
        duration=int(duration),
        imageUrl=logo,
        isFavorite=False,
        name=title,
        shuffle_disabled=True,
        repeat_disabled=True,
        streamType="onDemand",
    )
    return resp


def play_custom_stream(data: str) -> BmxPlaybackResponse:
    # data comes in as base64-encoded json with fields
    # streamUrl, imageUrl, and name
    json_str = base64.urlsafe_b64decode(data)
    json_obj = json.loads(json_str)
    stream_list = [
        Stream(
            hasPlaylist=True,
            isRealtime=True,
            streamUrl=json_obj["streamUrl"],
        )
    ]

    audio = Audio(
        hasPlaylist=True,
        isRealtime=True,
        streamUrl=json_obj["streamUrl"],
        streams=stream_list,
    )
    resp = BmxPlaybackResponse(
        audio=audio,
        imageUrl=json_obj["imageUrl"],
        name=json_obj["name"],
        streamType="liveRadio",
    )
    return resp

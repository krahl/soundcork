import logging
import os
import re
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi_etag import Etag

from soundcork.admin import get_admin_router
from soundcork.bmx import (
    play_custom_stream,
    tunein_navigation,
    tunein_playback,
    tunein_playback_podcast,
    tunein_podcast_info,
    tunein_root_navigation,
    tunein_search_navigation,
    tunein_token,
)
from soundcork.config import Settings
from soundcork.constants import ACCOUNT_RE, DEVICE_RE
from soundcork.datastore import DataStore
from soundcork.devices import (
    add_device,
    get_bose_devices,
    read_device_info,
    read_recents,
)
from soundcork.groups import get_groups_router
from soundcork.groups_service import get_groups_service_router
from soundcork.marge import (
    account_devices_xml,
    account_full_xml,
    account_presets_all_xml,
    account_sources_xml,
    add_device_to_account,
    add_recent,
    add_source_to_account,
    delete_preset,
    eligibility_xml,
    presets_xml,
    provider_settings_xml,
    recents_xml,
    remove_device_from_account,
    remove_source_from_account,
    rename_device,
    software_update_xml,
    source_providers,
    update_device_poweron,
    update_preset,
)
from soundcork.model import (
    BmxPlaybackResponse,
    BmxPodcastInfoResponse,
    BmxResponse,
    BoseXMLResponse,
)
from soundcork.ui.speakers import Speakers
from soundcork.utils import strip_element_text
from soundcork.spotify_service import SpotifyService, SpotifyTokenRefreshError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

datastore = DataStore()
settings = Settings()
speakers = Speakers(datastore, settings)
spotify_service = SpotifyService(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up soundcork")
    # datastore.discover_devices()
    logger.info("done starting up server")
    yield
    logger.debug("closing server")


description = """
This emulates the SoundTouch servers so you don't need connectivity
to use speakers.
"""

tags_metadata = [
    {
        "name": "marge",
        "description": "Communicates with the speaker.",
    },
    {
        "name": "service",
        "description": "Communicates with user applications.",
    },
    {
        "name": "bmx",
        "description": "Communicates with streaming radio services (eg. TuneIn).",
    },
]
app = FastAPI(
    title="SoundCork",
    description=description,
    summary="Emulates SoundTouch servers.",
    version="0.0.1",
    openapi_tags=tags_metadata,
    lifespan=lifespan,
)

origins = [
    "*",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# @lru_cache
# def get_settings():
#     return Settings()


startup_timestamp = int(datetime.now().timestamp() * 1000)


@app.get("/")
def read_root():
    return {"Bose": "Can't Brick Us"}


@app.post(
    "/marge/streaming/support/power_on",
    tags=["marge"],
)
async def power_on(request: Request, response: Response) -> Response:
    xml = await request.body()
    account = update_device_poweron(datastore, xml)
    if account:
        response.status_code = HTTPStatus.OK
        return response
    else:
        response = BoseXMLResponse()
        element = ET.Element("status")
        ET.SubElement(element, "message").text = "Device does not exist"
        ET.SubElement(element, "status-code").text = "4012"
        response.body = bose_xml_str(element).encode()
        response.headers["Content-Length"] = str(len(response.body))
        response.status_code = HTTPStatus.BAD_REQUEST
        return response


@app.get("/marge/streaming/sourceproviders", tags=["marge"])
def streamingsourceproviders():
    return_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sourceProviders>'
    )
    for provider in source_providers():
        return_xml = (
            return_xml
            + '<sourceprovider id="'
            + str(provider.id)
            + '">'
            + "<createdOn>"
            + provider.created_on
            + "</createdOn>"
            + "<name>"
            + provider.name
            + "</name>"
            + "<updatedOn>"
            + provider.updated_on
            + "</updatedOn>"
            "</sourceprovider>"
        )
    return_xml = return_xml + "</sourceProviders>"
    response = Response(content=return_xml, media_type="application/xml")
    # TODO: move content type to constants
    response.headers["content-type"] = "application/vnd.bose.streaming-v1.2+xml"
    # sourceproviders seems to return now as its etag
    etag = int(datetime.now().timestamp() * 1000)
    response.headers["etag"] = str(etag)
    return response


def etag_for_presets(request: Request) -> str:
    return str(datastore.etag_for_presets(str(request.path_params.get("account"))))


def etag_for_recents(request: Request) -> str:
    return str(datastore.etag_for_recents(str(request.path_params.get("account"))))


def etag_for_account(request: Request) -> str:
    return str(datastore.etag_for_account(str(request.path_params.get("account"))))


def etag_for_sources(request: Request) -> str:
    return str(datastore.etag_for_sources(str(request.path_params.get("account"))))


def etag_for_swupdate(request: Request) -> str:
    return "1663726921993"


def bose_xml_response(
    xml: ET.Element,
    *,
    version: str = "v1.2",
    headers: dict[str, str] | None = None,
) -> Response:
    response = Response(content=bose_xml_str(xml), media_type="application/xml")
    response.headers["content-type"] = f"application/vnd.bose.streaming-{version}+xml"
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


def stockholm_unauthorized_response() -> Response:
    return Response(
        content=(
            "<!doctype html><html lang=en><title>401 Unauthorized</title>"
            "<h1>Unauthorized</h1><p>Authorization not set. No access token found.</p>"
        ),
        media_type="text/html",
        status_code=HTTPStatus.UNAUTHORIZED,
    )


def filter_bmx_services(bmx_response: BmxResponse) -> BmxResponse:
    if settings.extended_bmx_registry:
        return bmx_response

    allowed_services = {"TUNEIN", "LOCAL_INTERNET_RADIO", "SIRIUSXM_EVEREST"}
    bmx_response.bmx_services = [
        service
        for service in bmx_response.bmx_services
        if service.id.name in allowed_services
    ]
    return bmx_response


def account_for_source_secret(source_type: str, secret: str) -> str | None:
    for account in datastore.list_accounts():
        try:
            configured_sources = datastore.get_configured_sources(account)
        except HTTPException:
            continue

        if any(
            source.source_key_type == source_type and source.secret == secret
            for source in configured_sources
        ):
            return account
    return None


def tunein_saved_preset_items(auth_token: str) -> list[dict]:
    account = account_for_source_secret("TUNEIN", auth_token)
    if not account:
        return []

    presets = datastore.get_presets(account, "")
    items = []
    for preset in presets:
        if preset.source != "TUNEIN" and not preset.location.startswith(
            "/v1/playback/station/"
        ):
            continue

        items.append(
            {
                "_links": {
                    "bmx_playback": {
                        "href": preset.location,
                        "type": preset.type or "stationurl",
                    },
                    "bmx_preset": {
                        "containerArt": preset.container_art,
                        "href": preset.location,
                        "name": preset.name,
                        "type": preset.type or "stationurl",
                    },
                },
                "imageUrl": preset.container_art,
                "name": preset.name,
                "subtitle": "",
            }
        )
    return items


async def spotify_token_refresh_response(provider_id: str, request: Request) -> Response:
    if provider_id != "15":
        return Response(status_code=HTTPStatus.NOT_FOUND)

    refresh_token = ""
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    if isinstance(payload, dict):
        raw_refresh_token = payload.get("refresh_token", "")
        if isinstance(raw_refresh_token, str):
            refresh_token = raw_refresh_token

    try:
        token = spotify_service.get_fresh_token_sync(refresh_token)
    except SpotifyTokenRefreshError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.payload)
    if not token:
        return JSONResponse(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            content={
                "error": "no_token",
                "error_description": "No Spotify account linked",
            },
        )

    return JSONResponse(
        content={
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": (
                "streaming user-read-email user-read-private "
                "playlist-read-private playlist-read-collaborative "
                "user-library-read"
            ),
        }
    )


@app.get(
    "/marge/streaming/account/{account}/device/{device}/presets",
    response_class=BoseXMLResponse,
    tags=["marge"],
    dependencies=[
        Depends(
            Etag(
                etag_gen=etag_for_presets,
                weak=False,
            )
        )
    ],
)
def account_presets(
    account: Annotated[str, Path(pattern=ACCOUNT_RE)],
    device: Annotated[str, Path(pattern=DEVICE_RE)],
    response: Response,
):
    xml = presets_xml(datastore, account, device)
    return bose_xml_str(xml)


@app.get(
    "/marge/streaming/account/{account}/presets/all",
    tags=["marge"],
    dependencies=[Depends(Etag(etag_gen=etag_for_presets, weak=False))],
)
def account_presets_all(account: Annotated[str, Path(pattern=ACCOUNT_RE)]) -> Response:
    xml = account_presets_all_xml(datastore, account)
    return bose_xml_response(xml, version="v1.1")


@app.put(
    "/marge/streaming/account/{account}/device/{device}/preset/{preset_number}",
    response_class=BoseXMLResponse,
    tags=["marge"],
    dependencies=[
        Depends(
            Etag(
                etag_gen=etag_for_presets,
                weak=False,
            )
        )
    ],
)
async def put_account_preset(
    account: Annotated[str, Path(pattern=ACCOUNT_RE)],
    device: Annotated[str, Path(pattern=DEVICE_RE)],
    preset_number: int,
    request: Request,
):
    xml = await request.body()
    xml_resp = update_preset(datastore, account, device, preset_number, xml)
    return bose_xml_str(xml_resp)


@app.delete(
    "/marge/streaming/account/{account}/device/{device}/preset/{preset_number}",
    response_class=BoseXMLResponse,
    tags=["marge"],
)
def delete_account_preset(
    account: Annotated[str, Path(pattern=ACCOUNT_RE)],
    device: Annotated[str, Path(pattern=DEVICE_RE)],
    preset_number: int,
):
    delete_preset(datastore, account, device, preset_number)
    return None


@app.get(
    "/marge/streaming/account/{account}/device/{device}/recents",
    response_class=BoseXMLResponse,
    tags=["marge"],
    dependencies=[
        Depends(
            Etag(
                etag_gen=etag_for_recents,
                weak=False,
            )
        )
    ],
)
def account_recents(
    account: Annotated[str, Path(pattern=ACCOUNT_RE)],
    device: Annotated[str, Path(pattern=DEVICE_RE)],
):
    xml = recents_xml(datastore, account, device)
    return bose_xml_str(xml)


@app.get(
    "/marge/streaming/account/{account}/devices",
    tags=["marge"],
    dependencies=[Depends(Etag(etag_gen=etag_for_account, weak=False))],
)
def account_devices(account: Annotated[str, Path(pattern=ACCOUNT_RE)]) -> Response:
    xml = account_devices_xml(datastore, account)
    return bose_xml_response(
        xml,
        version="v1.1",
        headers={"Method_name": "getDevices"},
    )


@app.get(
    "/marge/streaming/account/{account}/provider_settings",
    response_class=BoseXMLResponse,
    tags=["marge"],
    dependencies=[
        Depends(
            Etag(
                etag_gen=etag_for_recents,
                weak=False,
                extra_headers={"method_name": "getProviderSettings"},
            )
        )
    ],
)
def account_provider_settings(account: Annotated[str, Path(pattern=ACCOUNT_RE)]):
    xml = provider_settings_xml(account)
    return bose_xml_str(xml)


@app.post(
    "/marge/streaming/music/musicprovider/{provider_id}/is_eligible",
    tags=["marge"],
)
async def account_music_provider_eligibility(provider_id: str, request: Request) -> Response:
    await request.body()
    xml = eligibility_xml(False)
    return bose_xml_response(xml, version="v1.1")


@app.get(
    "/marge/streaming/software/update/account/{account}",
    response_class=BoseXMLResponse,
    dependencies=[Depends(Etag(etag_gen=etag_for_swupdate, weak=False))],
    tags=["marge"],
)
def software_update(account: Annotated[str, Path(pattern=ACCOUNT_RE)]):
    xml = software_update_xml()
    return bose_xml_str(xml)


@app.get(
    "/marge/streaming/account/{account}/full",
    response_class=BoseXMLResponse,
    tags=["marge"],
    dependencies=[
        Depends(
            Etag(
                etag_gen=etag_for_account,
                weak=False,
                extra_headers={"method_name": "getFullAccount"},
            )
        )
    ],
)
def account_full(account: Annotated[str, Path(pattern=ACCOUNT_RE)]) -> str:
    xml = account_full_xml(account, datastore)
    return bose_xml_str(xml)


@app.post(
    "/marge/streaming/account/{account}/device/{device}/recent",
    response_class=BoseXMLResponse,
    tags=["marge"],
    dependencies=[Depends(Etag(etag_gen=etag_for_recents, weak=False))],
)
async def post_account_recent(
    account: Annotated[str, Path(pattern=ACCOUNT_RE)],
    device: Annotated[str, Path(pattern=DEVICE_RE)],
    request: Request,
):
    xml = await request.body()
    xml_resp = add_recent(datastore, account, device, xml)
    return bose_xml_str(xml_resp)


@app.post(
    "/marge/streaming/account/{account}/device/",
    response_class=BoseXMLResponse,
    tags=["marge"],
    status_code=HTTPStatus.CREATED,
    dependencies=[
        Depends(
            Etag(
                etag_gen=etag_for_account,
                weak=False,
                extra_headers={
                    "method_name": "addDevice",
                    "access-control-expose-headers": "Credentials",
                },
            )
        )
    ],
)
async def post_account_device(
    account: Annotated[str, Path(pattern=ACCOUNT_RE)],
    request: Request,
):
    xml = await request.body()
    device_id, xml_resp = add_device_to_account(datastore, account, xml.decode())

    return bose_xml_str(xml_resp)


@app.put(
    "/marge/streaming/account/{account}/device/{device_id}",
    response_class=BoseXMLResponse,
    tags=["marge"],
    status_code=HTTPStatus.CREATED,
    dependencies=[
        Depends(
            Etag(
                etag_gen=etag_for_account,
                weak=False,
                extra_headers={
                    "method_name": "putDevice",
                },
            )
        )
    ],
)
async def put_account_device(
    account: Annotated[str, Path(pattern=ACCOUNT_RE)],
    device_id: Annotated[str, Path(pattern=DEVICE_RE)],
    request: Request,
):
    xml = await request.body()
    xml_resp = rename_device(datastore, account, device_id, xml.decode())

    return bose_xml_str(xml_resp)


@app.delete("/marge/streaming/account/{account}/device/{device}", tags=["marge"])
async def delete_account_device(
    account: Annotated[str, Path(pattern=ACCOUNT_RE)],
    device: Annotated[str, Path(pattern=DEVICE_RE)],
    response: Response,
):
    xml_resp = remove_device_from_account(datastore, account, device)
    response.headers["method_name"] = "removeDevice"
    response.headers["location"] = (
        f"{settings.base_url}/marge/account/{account}/device/{device}"
    )
    response.body = b""
    response.status_code = HTTPStatus.OK
    return response


@app.get("/marge/streaming/device/{device_id}/streaming_token", tags=["marge"])
def streaming_token(device_id: str, response: Response):
    response.headers["Authorization"] = "c3dvcmRmaXNoCg=="
    etag = int(datetime.now().timestamp() * 1000)
    response.headers["ETag"] = str(etag)

    return


@app.post("/marge/streaming/account/login", tags=["marge"])
async def post_account_login(
    request: Request,
):
    xml = await request.body()
    # for now if they send in an account id as the username
    # then log in that account
    try:
        login_xml = ET.fromstring(xml)
        if login_xml:
            username = strip_element_text(login_xml.find("username"))
            account_pattern = re.compile(ACCOUNT_RE)
            if account_pattern.match(username):
                account_id = username
            else:
                raise Exception
    except Exception:
        exception_xml = """<status>
        <message>Account Login failure.</message>
        <status-code>4024</status-code>
        </status>"""
        response = Response(content=exception_xml, media_type="application/xml")
        response.status_code = HTTPStatus.BAD_REQUEST
        return response

    account_elem = ET.Element("account")
    account_elem.attrib["id"] = account_id
    ET.SubElement(account_elem, "accountStatus").text = "OK"
    ET.SubElement(account_elem, "mode").text = "global"
    ET.SubElement(account_elem, "preferredLanguage").text = "en"

    return_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>{ET.tostring(account_elem, encoding="unicode")}'
    response = Response(content=return_xml, media_type="application/xml")
    # TODO: move content type to constants
    response.headers["content-type"] = "application/vnd.bose.streaming-v1.2+xml"

    etag = startup_timestamp

    response.headers["etag"] = str(etag)
    # just making this up
    response.headers["Credentials"] = "3432143243243432143fdafd"
    return response


@app.post(
    "/marge/oauth/account/{account}/music/musicprovider/{provider_id}/token/{client_id}",
    tags=["oauth"],
)
async def stockholm_oauth_token_refresh(
    account: Annotated[str, Path(pattern=ACCOUNT_RE)],
    provider_id: str,
    client_id: str,
    request: Request,
) -> Response:
    return await spotify_token_refresh_response(provider_id, request)


@app.post(
    "/oauth/device/{device_id}/music/musicprovider/{provider_id}/token/{token_type}",
    tags=["oauth"],
)
async def speaker_oauth_token_refresh(
    device_id: str,
    provider_id: str,
    token_type: str,
    request: Request,
) -> Response:
    return await spotify_token_refresh_response(provider_id, request)


@app.get(
    "/marge/streaming/account/{account}/sources",
    response_class=BoseXMLResponse,
    tags=["marge"],
    dependencies=[
        Depends(
            Etag(
                etag_gen=etag_for_sources,
                weak=False,
            )
        )
    ],
)
def get_account_sources(account: Annotated[str, Path(pattern=ACCOUNT_RE)]) -> str:
    xml = account_sources_xml(account, datastore)
    return bose_xml_str(xml)


@app.post(
    "/marge/streaming/account/{account}/source",
    response_class=BoseXMLResponse,
    tags=["marge"],
    status_code=HTTPStatus.CREATED,
    dependencies=[
        Depends(
            Etag(
                etag_gen=etag_for_account,
                weak=False,
                extra_headers={
                    "method_name": "addSource",
                },
            )
        )
    ],
)
async def post_account_source(
    account: Annotated[str, Path(pattern=ACCOUNT_RE)],
    request: Request,
):
    xml = await request.body()
    xml_resp = add_source_to_account(datastore, account, xml.decode())

    return bose_xml_str(xml_resp)


@app.delete("/marge/streaming/account/{account}/source/{source_id}", tags=["marge"])
async def delete_account_source(
    account: Annotated[str, Path(pattern=ACCOUNT_RE)],
    source_id: str,
    response: Response,
):
    remove_source_from_account(datastore, account, source_id)
    response.headers["method_name"] = "removeSource"
    response.headers["location"] = (
        f"{settings.base_url}/marge/account/{account}/source/{source_id}"
    )
    response.body = b""
    response.status_code = HTTPStatus.OK
    return response


@app.get("/bmx/registry/v1/services", response_model_exclude_none=True, tags=["bmx"])
def bmx_services() -> BmxResponse:

    with open("bmx_services.json", "r") as file:
        bmx_response_json = file.read()
        bmx_response_json = bmx_response_json.replace(
            "{MEDIA_SERVER}", f"{settings.base_url}/media"
        ).replace("{BMX_SERVER}", settings.base_url)
        # TODO:  we're sending askAgainAfter hardcoded, but that value actually
        # varies.
        bmx_response = BmxResponse.model_validate_json(bmx_response_json)
        return filter_bmx_services(bmx_response)


@app.post("/bmx/tunein/v1/token", tags=["bmx"])
async def bmx_tunein_token(request: Request) -> dict:
    payload = await request.json()
    refresh_token = payload.get("refresh_token", "")
    if not refresh_token:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="refresh_token required")
    return tunein_token(refresh_token)


@app.get("/bmx/tunein/v1/navigate", tags=["bmx"], response_model=None)
def bmx_tunein_navigate(request: Request):
    auth_token = request.headers.get("Authorization", "")
    return tunein_root_navigation(
        auth_token,
        favorites_items=tunein_saved_preset_items(auth_token),
    )


@app.get(
    "/bmx/tunein/v1/navigate/{encoded_target:path}",
    tags=["bmx"],
    response_model=None,
)
def bmx_tunein_navigate_target(encoded_target: str, request: Request):
    auth_token = request.headers.get("Authorization", "")

    try:
        return tunein_navigation(
            encoded_target,
            auth_token,
            favorites_items=tunein_saved_preset_items(auth_token),
        )
    except ValueError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc))


@app.get("/bmx/tunein/v1/search", tags=["bmx"], response_model=None)
def bmx_tunein_search(request: Request):
    auth_token = request.headers.get("Authorization", "")
    query = request.query_params.get("q", "")
    return tunein_search_navigation(query, auth_token)


@app.post("/bmx/tunein/v1/report", tags=["bmx"], status_code=HTTPStatus.OK)
async def bmx_tunein_report(request: Request):
    await request.body()
    return


@app.get(
    "/bmx/tunein/v1/playback/station/{station_id}",
    response_model_exclude_none=True,
    tags=["bmx"],
)
def bmx_playback(station_id: str) -> BmxPlaybackResponse:
    return tunein_playback(station_id)


@app.get(
    "/bmx/tunein/v1/playback/episodes/{episode_id}",
    response_model_exclude_none=True,
    tags=["bmx"],
)
def bmx_podcast_info(episode_id: str, request: Request) -> BmxPodcastInfoResponse:
    encoded_name = request.query_params.get("encoded_name", "")
    return tunein_podcast_info(episode_id, encoded_name)


@app.get(
    "/bmx/tunein/v1/playback/episode/{episode_id}",
    response_model_exclude_none=True,
    tags=["bmx"],
)
def bmx_playback_podcast(episode_id: str, request: Request) -> BmxPlaybackResponse:
    return tunein_playback_podcast(episode_id)


@app.get("/core02/svc-bmx-adapter-orion/prod/orion/station", tags=["bmx"])
def custom_stream_playback(request: Request) -> BmxPlaybackResponse:
    data = request.query_params.get("data", "")
    return play_custom_stream(data)


@app.get("/media/{filename}", tags=["bmx"])
def bmx_media_file(filename: str) -> FileResponse:
    sanitized_filename = "".join(
        x for x in filename if x.isalnum() or x == "." or x == "-" or x == "_"
    )
    file_path = os.path.join("media", sanitized_filename)
    if os.path.isfile(file_path):
        return FileResponse(file_path)

    raise HTTPException(status_code=404, detail="not found")


@app.get("/updates/soundtouch", tags=["swupdate"])
def sw_update() -> Response:
    with open("swupdate.xml", "r") as file:
        sw_update_response = file.read()
        response = Response(content=sw_update_response, media_type="application/xml")
        return response


@app.post("/v1/scmudc/{deviceid}", tags=["stats"], status_code=HTTPStatus.OK)
def stats_scmudc(deviceid: str):
    """Returns 200 for the analytics endpoint.

    This isn't an endpoint we use, but it's noisy when it fails. Return 200.
    """
    return


def bose_xml_str(xml: ET.Element) -> str:
    # ET.tostring won't allow you to set standalone="yes"
    return_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>{ET.tostring(xml, encoding="unicode")}'

    return return_xml


################## configuration ############


@app.get("/scan_recents", tags=["setup"])
def test_scan_recents():
    devices = get_bose_devices()
    recents = []
    for device in devices:
        recents.append(read_recents(device))
    return recents


@app.get("/scan", tags=["setup"])
def scan_devices():
    """Unlikely to be used in production, but has been useful during development."""
    devices = get_bose_devices()
    device_infos = {}
    for device in devices:
        info_elem = ET.fromstring(read_device_info(device))
        device_infos[device.udn] = {
            "device_id": info_elem.attrib.get("deviceID", ""),
            "name": info_elem.find("name").text,  # type: ignore
            "type": info_elem.find("type").text,  # type: ignore
            "marge URL": info_elem.find("margeURL").text,  # type: ignore
            "account": info_elem.find("margeAccountUUID").text,  # type: ignore
        }
    return device_infos


@app.post("/add_device/{device_id}", tags=["setup"])
def add_device_to_datastore(device_id: str):
    devices = get_bose_devices()
    for device in devices:
        info_elem = ET.fromstring(read_device_info(device))
        if info_elem.attrib.get("deviceID", "") == device_id:
            success = add_device(device)
            return {device_id: success}


#####################################################################################
# -- include all routines for groups
app.include_router(get_groups_router(datastore))
app.include_router(get_groups_service_router(datastore))


# -- include admin router
app.include_router(get_admin_router(datastore, speakers))

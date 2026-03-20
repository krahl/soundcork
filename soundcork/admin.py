"""
Endpoints for an admin UI.
"""

import logging

from fastapi import APIRouter, Request

from soundcork.datastore import DataStore

router = APIRouter(tags=["admin"])

logger = logging.getLogger(__name__)


def get_admin_router(datastore: DataStore, settings):
    from bosesoundtouchapi.soundtouchclient import SoundTouchDevice  # type: ignore
    from bosesoundtouchapi.soundtouchdiscovery import SoundTouchDiscovery  # type:ignore
    from fastapi.responses import HTMLResponse
    from fastapi.templating import Jinja2Templates

    st_discovery = SoundTouchDiscovery()
    st_discovery.DiscoverDevices(timeout=1)

    templates = Jinja2Templates(directory="templates")

    router = APIRouter(tags=["admin"])

    @router.get("/admin/", response_class=HTMLResponse)
    async def admin(request: Request):
        discovered = st_discovery.DiscoveredDeviceNames

        devices = []
        for ip in discovered.keys():
            device = {}
            device["ip_addr"] = ip[:-5]

            device["name"] = discovered.get(ip, "")
            st_device = SoundTouchDevice(device["ip_addr"])

            device["status"] = (
                f"{st_device.DeviceId} {st_device.StreamingAccountUUID} {st_device.StreamingUrl}"
            )
            devices.append(device)

        account_ids = datastore.list_accounts()
        accounts = []
        for account_id in account_ids:
            account = {}
            account["account_id"] = account_id
            accounts.append(account)

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"accounts": accounts, "devices": devices},
        )

    return router

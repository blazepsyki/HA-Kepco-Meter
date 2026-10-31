"""First-setup session handoff with the real auth and client over a mock KEPCO server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, CookieJar, TCPConnector, web
from aresponses import Response, ResponsesMockServer
from custom_components.kepco_on.const import (
    CONF_DISPLAY_NAME,
    CONF_SAVE_PASSWORD,
    CONF_SELECTED_CUSTOMERS,
    CONF_SESSION_HANDOFF,
    CONF_USERNAME,
)
from homeassistant.const import CONF_PASSWORD

from .test_auth import HOST, html_response, json_response
from .test_config_flow import attach_fake_hass

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_user_step_hands_off_tokens_rotated_by_session_check_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on.config_flow as config_flow

    connector = TCPConnector()
    sessions: list[ClientSession] = []

    def make_session(hass: Any, *, auto_cleanup: bool, cookie_jar: CookieJar) -> ClientSession:
        del hass, auto_cleanup
        session = ClientSession(connector=connector, connector_owner=False, cookie_jar=cookie_jar)
        sessions.append(session)
        return session

    monkeypatch.setattr(config_flow, "async_create_clientsession", make_session)

    is_corp_calls = 0

    async def is_corp(request: web.Request) -> Response:
        nonlocal is_corp_calls
        del request
        is_corp_calls += 1
        if is_corp_calls == 1:
            # Cold session right after login: the protected endpoint answers 401 once.
            return json_response({"expired": True}, status=401)
        return json_response({"userClNm": "INDI"})

    customers = json.loads((FIXTURES / "customer_list_single.json").read_text(encoding="utf-8"))
    server = ResponsesMockServer()
    server.add(HOST, "/MYM001D00", "get", response=html_response(), repeat=10)
    server.add(
        HOST,
        "/cyb/me/login/indi/api",
        "post",
        response=json_response(
            {
                "dma_loginData2": {
                    "result": "YES",
                    "token": "LOGIN_TOKEN",
                    "refreshToken": "LOGIN_REFRESH",
                    "userId": "USER_ID",
                    "mbrsNm": "MEMBER",
                }
            }
        ),
    )
    server.add(HOST, "/isCorp", "post", response=is_corp, repeat=2)
    server.add(
        HOST,
        "/sessionCheck",
        "post",
        response=json_response(
            {
                "result": True,
                "refreshToken": "ROTATED_REFRESH",
                "token": "ROTATED_TOKEN",
                "userId": "USER_ID",
                "mbrsNm": "MEMBER",
            }
        ),
    )
    server.add(
        HOST,
        "/my/indi/info/myPageCustNoList",
        "post",
        response=json_response(customers),
    )

    flow = config_flow.KepcoOnConfigFlow()
    attach_fake_hass(flow)
    flow.context = {"source": "user"}
    flow.flow_id = "flow-1"
    try:
        async with server:
            shown = await flow.async_step_user(
                {
                    CONF_USERNAME: "user",
                    CONF_PASSWORD: "pw",
                    CONF_SAVE_PASSWORD: False,
                    CONF_DISPLAY_NAME: "",
                }
            )
            assert shown["type"] == "form", shown
            assert shown["step_id"] == "customer"
            assert flow._pending is not None
            key = flow._pending.customers[0].stable_key
            created = await flow.async_step_customer({CONF_SELECTED_CUSTOMERS: [key]})
    finally:
        for session in sessions:
            if not session.closed:
                session.detach()
        await connector.close()

    assert is_corp_calls == 2
    assert created["type"] == "create_entry"
    handoff = created["data"][CONF_SESSION_HANDOFF]
    assert handoff["refresh_token"] == "ROTATED_REFRESH"
    assert handoff["token"] == "ROTATED_TOKEN"

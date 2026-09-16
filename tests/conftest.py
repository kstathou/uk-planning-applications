# Copyright (c) 2026 Kostas Stathoulopoulos

from __future__ import annotations

import httpx
import pytest


@pytest.fixture(autouse=True)
def _label_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.MockTransport.handle_async_request

    async def labeled(
        transport: httpx.MockTransport,
        request: httpx.Request,
    ) -> httpx.Response:
        response = await original(transport, request)
        if "content-type" not in response.headers:
            response.headers["content-type"] = "text/html"
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", labeled)

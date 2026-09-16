# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ANN401, D103, PLR2004

"""Production Blackburn Playwright page-object boundaries."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import yimby.authorities.blackburn_with_darwen.page_object as blackburn_page
from yimby.authorities.blackburn_with_darwen.adapter import (
    BlackburnDateRangeV1,
    BlackburnLocatorV1,
    BlackburnQueryKind,
    BlackburnQueryV1,
)
from yimby.authorities.blackburn_with_darwen.page_object import (
    BlackburnPlaywrightSession,
)
from yimby.browser_transport import PlaywrightBoundary

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from playwright.async_api import Page


class _InteractiveBoundary:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.closed = False

    async def interact[T](self, operation: Callable[[Page], Awaitable[T]]) -> T:
        return await operation(self.page)

    async def open(self, _url: str) -> Any:
        raise AssertionError

    async def aclose(self) -> None:
        self.closed = True


def _query(kind: BlackburnQueryKind) -> BlackburnQueryV1:
    return BlackburnQueryV1(
        kind=kind,
        date_range=BlackburnDateRangeV1(
            start=date(2026, 8, 18),
            end=date(2026, 9, 16),
        ),
    )


def _control(*, value: str | None = None) -> MagicMock:
    control = MagicMock()
    control.count = AsyncMock(return_value=1)
    control.wait_for = AsyncMock()
    control.fill = AsyncMock()
    control.get_attribute = AsyncMock(return_value=value)
    return control


def _search_page() -> MagicMock:
    page = MagicMock()
    page.url = "https://online.blackburn.gov.uk/planning/index.html"
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.content = AsyncMock(return_value="<html>rendered results</html>")
    form = _control()
    form.get_attribute = AsyncMock(
        side_effect=lambda name: {
            "method": "post",
            "action": "/planning/index.html",
        }[name]
    )
    controls = {
        'input[name="fa"]': _control(value=""),
        'input[name="submitted"]': _control(value=""),
        'input[name="received_date_from"]': _control(),
        'input[name="received_date_to"]': _control(),
        'input[name="valid_date_from"]': _control(),
        'input[name="valid_date_to"]': _control(),
        'input[name="decision_issued_date_from"]': _control(),
        'input[name="decision_issued_date_to"]': _control(),
    }
    page.locator.side_effect = lambda selector: (
        form if selector == "form#form" else controls[selector]
    )
    search = MagicMock()
    search.count = AsyncMock(return_value=1)
    search.click = AsyncMock()
    page.get_by_role.return_value = search
    page.form = form
    page.controls = controls
    page.search = search
    return page


def _detail_page(*, count: int = 1) -> MagicMock:
    page = MagicMock()
    page.url = (
        "https://online.blackburn.gov.uk/planning/"
        "index.html?fa=getApplication&id=178041"
    )
    page.goto = AsyncMock()
    page.content = AsyncMock(return_value="<div id='application_details'></div>")
    application = MagicMock()
    application.count = AsyncMock(return_value=count)
    application.wait_for = AsyncMock()
    page.locator.return_value = application
    page.application = application
    return page


@pytest.mark.parametrize(
    ("kind", "start_name", "end_name"),
    [
        (
            BlackburnQueryKind.RECEIVED,
            "received_date_from",
            "received_date_to",
        ),
        (BlackburnQueryKind.VALID, "valid_date_from", "valid_date_to"),
        (
            BlackburnQueryKind.DECISION,
            "decision_issued_date_from",
            "decision_issued_date_to",
        ),
        (
            BlackburnQueryKind.OLDER_OPEN,
            "received_date_from",
            "received_date_to",
        ),
    ],
)
def test_blackburn_page_object_submits_exact_hyphen_date_fields(
    kind: BlackburnQueryKind,
    start_name: str,
    end_name: str,
) -> None:
    page = _search_page()
    pause = AsyncMock()
    session = BlackburnPlaywrightSession(
        _InteractiveBoundary(cast("Page", page)),
        pause=pause,
    )

    capture = asyncio.run(session.search(_query(kind)))

    page.goto.assert_awaited_once_with(
        "https://online.blackburn.gov.uk/planning/index.html?fa=search",
        wait_until="domcontentloaded",
    )
    page.form.wait_for.assert_awaited_once_with(state="attached", timeout=20_000)
    page.controls[f'input[name="{start_name}"]'].fill.assert_awaited_once_with(
        "18-08-2026"
    )
    page.controls[f'input[name="{end_name}"]'].fill.assert_awaited_once_with(
        "16-09-2026"
    )
    page.get_by_role.assert_called_once_with("button", name="Search", exact=True)
    page.search.click.assert_awaited_once_with()
    page.wait_for_load_state.assert_awaited_once_with("domcontentloaded")
    assert pause.await_count == 2
    assert capture.body == b"<html>rendered results</html>"
    assert session.requested_urls == (
        "https://online.blackburn.gov.uk/planning/index.html",
    )


def test_blackburn_page_object_opens_detail_without_document_actions() -> None:
    page = _detail_page()
    pause = AsyncMock()
    session = BlackburnPlaywrightSession(
        _InteractiveBoundary(cast("Page", page)),
        pause=pause,
    )
    locator = BlackburnLocatorV1(
        record_id="178041",
        public_reference="10/26/0747",
    )

    capture = asyncio.run(session.application(locator))

    page.goto.assert_awaited_once_with(
        "https://online.blackburn.gov.uk/planning/"
        "index.html?fa=getApplication&id=178041",
        wait_until="domcontentloaded",
    )
    page.locator.assert_called_once_with(
        '#application_details[data-application-id="178041"]'
    )
    page.application.wait_for.assert_awaited_once_with(timeout=20_000)
    page.get_by_role.assert_not_called()
    assert pause.await_count == 1
    assert capture.body == b"<div id='application_details'></div>"
    assert session.attachment_body_requests == 0


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("method", "get"),
        ("action", "/wrong"),
    ],
)
def test_blackburn_page_object_rejects_form_drift(
    attribute: str,
    value: str,
) -> None:
    page = _search_page()
    original = {"method": "post", "action": "/planning/index.html"}
    original[attribute] = value
    page.form.get_attribute = AsyncMock(side_effect=lambda name: original[name])
    session = BlackburnPlaywrightSession(
        _InteractiveBoundary(cast("Page", page)),
        pause=AsyncMock(),
    )

    with pytest.raises(blackburn_page.BlackburnPageObjectFormError):
        asyncio.run(session.search(_query(BlackburnQueryKind.RECEIVED)))


def test_blackburn_page_object_rejects_missing_form_controls() -> None:
    def rejected(page: MagicMock) -> None:
        session = BlackburnPlaywrightSession(
            _InteractiveBoundary(cast("Page", page)),
            pause=AsyncMock(),
        )
        with pytest.raises(blackburn_page.BlackburnPageObjectFormError):
            asyncio.run(session.search(_query(BlackburnQueryKind.RECEIVED)))

    missing_form = _search_page()
    missing_form.form.count = AsyncMock(return_value=0)
    rejected(missing_form)

    missing_hidden = _search_page()
    missing_hidden.controls['input[name="fa"]'].count = AsyncMock(return_value=0)
    rejected(missing_hidden)

    changed_hidden = _search_page()
    changed_hidden.controls['input[name="submitted"]'].get_attribute = AsyncMock(
        return_value="changed"
    )
    rejected(changed_hidden)

    missing_date = _search_page()
    missing_date.controls['input[name="received_date_from"]'].count = AsyncMock(
        return_value=0
    )
    rejected(missing_date)

    missing_button = _search_page()
    missing_button.search.count = AsyncMock(return_value=0)
    rejected(missing_button)


def test_blackburn_page_object_accepts_valueless_empty_hidden_controls() -> None:
    page = _search_page()
    page.controls['input[name="fa"]'].get_attribute = AsyncMock(return_value=None)
    page.controls['input[name="submitted"]'].get_attribute = AsyncMock(
        return_value=None
    )
    session = BlackburnPlaywrightSession(
        _InteractiveBoundary(cast("Page", page)),
        pause=AsyncMock(),
    )

    asyncio.run(session.search(_query(BlackburnQueryKind.RECEIVED)))

    page.search.click.assert_awaited_once_with()


def test_blackburn_page_object_accepts_live_hidden_search_discriminators() -> None:
    page = _search_page()
    page.controls['input[name="fa"]'].get_attribute = AsyncMock(return_value="search")
    page.controls['input[name="submitted"]'].get_attribute = AsyncMock(
        return_value="true"
    )
    session = BlackburnPlaywrightSession(
        _InteractiveBoundary(cast("Page", page)),
        pause=AsyncMock(),
    )

    asyncio.run(session.search(_query(BlackburnQueryKind.RECEIVED)))

    page.search.click.assert_awaited_once_with()


def test_blackburn_page_object_rejects_ambiguous_detail_routes() -> None:
    session = BlackburnPlaywrightSession(
        _InteractiveBoundary(cast("Page", _detail_page())),
        pause=AsyncMock(),
    )
    with pytest.raises(blackburn_page.BlackburnPageObjectRouteError):
        asyncio.run(
            session.application(
                BlackburnLocatorV1(
                    record_id="not-numeric",
                    public_reference="10/26/0747",
                )
            )
        )

    timed_out = _detail_page()
    timed_out.application.wait_for = AsyncMock(
        side_effect=PlaywrightTimeoutError("sanitised timeout")
    )
    body = MagicMock()
    body.inner_text = AsyncMock(return_value="ordinary error page")
    timed_out.locator.side_effect = lambda selector: (
        body if selector == "body" else timed_out.application
    )
    session = BlackburnPlaywrightSession(
        _InteractiveBoundary(cast("Page", timed_out)),
        pause=AsyncMock(),
    )
    with pytest.raises(blackburn_page.BlackburnPageObjectRouteError):
        asyncio.run(
            session.application(
                BlackburnLocatorV1(
                    record_id="178041",
                    public_reference="10/26/0747",
                )
            )
        )

    ambiguous = _detail_page(count=2)
    session = BlackburnPlaywrightSession(
        _InteractiveBoundary(cast("Page", ambiguous)),
        pause=AsyncMock(),
    )
    with pytest.raises(blackburn_page.BlackburnPageObjectRouteError):
        asyncio.run(
            session.application(
                BlackburnLocatorV1(
                    record_id="178041",
                    public_reference="10/26/0747",
                )
            )
        )


def test_blackburn_page_object_names_human_verification_blocker() -> None:
    page = _detail_page()
    page.application.wait_for = AsyncMock(
        side_effect=PlaywrightTimeoutError("sanitised timeout")
    )
    body = MagicMock()
    body.inner_text = AsyncMock(
        return_value="Let's confirm you are human Complete the security check"
    )
    page.locator.side_effect = lambda selector: (
        body if selector == "body" else page.application
    )
    session = BlackburnPlaywrightSession(
        _InteractiveBoundary(cast("Page", page)),
        pause=AsyncMock(),
    )

    with pytest.raises(blackburn_page.BlackburnHumanVerificationRequiredError):
        asyncio.run(
            session.application(
                BlackburnLocatorV1(
                    record_id="178041",
                    public_reference="10/26/0747",
                )
            )
        )


def test_blackburn_page_object_factory_and_default_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _search_page()
    boundary = _InteractiveBoundary(cast("Page", page))
    create = AsyncMock(return_value=boundary)
    sleep = AsyncMock()
    monkeypatch.setattr(PlaywrightBoundary, "create", create)
    monkeypatch.setattr(
        "yimby.authorities.blackburn_with_darwen.page_object.asyncio.sleep",
        sleep,
    )

    async def exercise() -> None:
        session = await BlackburnPlaywrightSession.create()
        await session.search(_query(BlackburnQueryKind.RECEIVED))
        await session.aclose()

    asyncio.run(exercise())

    create.assert_awaited_once_with(headless=False)
    assert sleep.await_count == 2
    sleep.assert_awaited_with(2.0)
    assert boundary.closed

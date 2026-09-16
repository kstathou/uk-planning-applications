# Copyright (c) 2026 Kostas Stathoulopoulos

"""Guarded Playwright page object for Blackburn's Citizen portal."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import urljoin, urlsplit

from yimby.browser_transport import (
    BrowserBoundary,
    BrowserWorker,
    PlaywrightBoundary,
    PlaywrightPortalSession,
)

from .adapter import (
    BASE_URL,
    BlackburnLocatorV1,
    BlackburnQueryKind,
    BlackburnQueryV1,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from playwright.async_api import Page

    from yimby.domain import EvidenceCapture

_SEARCH_URL = f"{BASE_URL}/index.html?fa=search"
_FORM_PATH = "/planning/index.html"
_MINIMUM_GAP_SECONDS = 2.0
_DATE_FIELDS = {
    BlackburnQueryKind.RECEIVED: ("received_date_from", "received_date_to"),
    BlackburnQueryKind.VALID: ("valid_date_from", "valid_date_to"),
    BlackburnQueryKind.DECISION: (
        "decision_issued_date_from",
        "decision_issued_date_to",
    ),
    BlackburnQueryKind.OLDER_OPEN: ("received_date_from", "received_date_to"),
}


class BlackburnPlaywrightSession(PlaywrightPortalSession):
    """Submit verified portal forms through the guarded browser boundary."""

    def __init__(
        self,
        boundary: BrowserBoundary,
        *,
        worker: BrowserWorker | None = None,
        pause: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Bind the browser lifecycle and injectable two-second pacer."""
        super().__init__(boundary, worker=worker)
        self._pause = pause or _minimum_gap

    @classmethod
    async def create(
        cls,
        *,
        worker: BrowserWorker | None = None,
    ) -> BlackburnPlaywrightSession:
        """Launch the shared guarded Playwright boundary."""
        return cls(await PlaywrightBoundary.create(), worker=worker)

    async def search(self, query: BlackburnQueryV1) -> EvidenceCapture:
        """Submit one exact inclusive date query."""
        return await self.run_browser(lambda page: self._search(page, query))

    async def application(self, locator: BlackburnLocatorV1) -> EvidenceCapture:
        """Render detail and inline document metadata without link activation."""
        return await self.run_browser(lambda page: self._application(page, locator))

    async def _search(
        self,
        page: Page,
        query: BlackburnQueryV1,
    ) -> EvidenceCapture:
        await self._pause()
        await page.goto(_SEARCH_URL, wait_until="domcontentloaded")
        await _assert_search_form(page)
        start_name, end_name = _DATE_FIELDS[query.kind]
        await _fill_date(page, start_name, query.date_range.start.strftime("%d-%m-%Y"))
        await _fill_date(page, end_name, query.date_range.end.strftime("%d-%m-%Y"))
        search = page.get_by_role("button", name="Search", exact=True)
        if await search.count() != 1:
            return _raise_form("single Search button")
        await self._pause()
        await search.click()
        await page.wait_for_load_state("domcontentloaded")
        body = (await page.content()).encode()
        return self.retain_rendered(page.url, body)

    async def _application(
        self,
        page: Page,
        locator: BlackburnLocatorV1,
    ) -> EvidenceCapture:
        if not locator.record_id.isdigit():
            return _raise_route(locator.record_id)
        await self._pause()
        url = f"{BASE_URL}/index.html?fa=getApplication&id={locator.record_id}"
        await page.goto(url, wait_until="domcontentloaded")
        application = page.locator(
            f'#application_details[data-application-id="{locator.record_id}"]'
        )
        await application.wait_for()
        if await application.count() != 1:
            return _raise_route(locator.record_id)
        body = (await page.content()).encode()
        return self.retain_rendered(page.url, body)


async def _assert_search_form(page: Page) -> None:
    form = page.locator("form#form")
    if await form.count() != 1:
        _raise_form("single search form")
    method = (await form.get_attribute("method") or "").casefold()
    action = await form.get_attribute("action") or ""
    resolved = urlsplit(urljoin(page.url, action))
    expected = urlsplit(BASE_URL)
    if (
        method != "post"
        or resolved.scheme != expected.scheme
        or resolved.netloc != expected.netloc
        or resolved.path != _FORM_PATH
        or resolved.query
    ):
        _raise_form("POST /planning/index.html form")
    for name in ("fa", "submitted"):
        control = page.locator(f'input[name="{name}"]')
        if await control.count() != 1 or await control.get_attribute("value") != "":
            _raise_form(f"empty hidden {name} control")


async def _fill_date(page: Page, name: str, value: str) -> None:
    control = page.locator(f'input[name="{name}"]')
    if await control.count() != 1:
        _raise_form(f"single {name} control")
    await control.fill(value)


async def _minimum_gap() -> None:
    await asyncio.sleep(_MINIMUM_GAP_SECONDS)


class BlackburnPageObjectFormError(RuntimeError):
    """The rendered search form no longer matches observed semantics."""

    def __init__(self, field: str) -> None:
        """Name the safe form contract that drifted."""
        super().__init__(f"Blackburn browser form mismatch: {field}")


class BlackburnPageObjectRouteError(RuntimeError):
    """A numeric application route was absent or ambiguous."""

    def __init__(self, record_id: str) -> None:
        """Name the safe record identifier."""
        super().__init__(f"Blackburn browser route mismatch: {record_id}")


def _raise_form(field: str) -> NoReturn:
    raise BlackburnPageObjectFormError(field)


def _raise_route(record_id: str) -> NoReturn:
    raise BlackburnPageObjectRouteError(record_id)

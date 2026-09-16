# Copyright (c) 2026 Kostas Stathoulopoulos

"""Production Playwright page object for Haringey's Arcus register."""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import ceil
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import parse_qs, urljoin, urlsplit

from pydantic import HttpUrl

from yimby.browser_transport import (
    BrowserWorker,
    PlaywrightBoundary,
    PlaywrightPortalSession,
)

from .adapter import (
    BASE_URL,
    QUICK_LINK_NAME,
    REGISTER_NAME,
    HaringeyApplicationPagesV1,
    HaringeyLocatorV1,
    HaringeySearchHitV1,
    HaringeySearchPageV1,
    _parse_date,
)

if TYPE_CHECKING:
    from datetime import date

    from playwright.async_api import Page

_QUICK_LINK_BUTTON = "Planning Applications Validated in last 7 days"
_RESULT_CARD = ".slds-form.slds-box"
_RESULT_LINK = 'a[href*="/pr/s/detail/"]'
_RESULT_COUNT = ".pr-pagination__results"
_PAGE_SIZE = 10
_MAX_PAGE_SETS = 20


class HaringeyPlaywrightSession(PlaywrightPortalSession):
    """Portal session implementing Haringey's semantic page-object protocol."""

    @classmethod
    async def create(
        cls, *, worker: BrowserWorker | None = None
    ) -> HaringeyPlaywrightSession:
        """Launch the shared guarded Playwright boundary."""
        return cls(await PlaywrightBoundary.create(), worker=worker)

    async def validated_last_seven_days(self, page_number: int) -> HaringeySearchPageV1:
        """Open the verified quick link and return one reconciled result page."""
        return await self.run_browser(
            lambda page: self._validated_page(page, page_number)
        )

    async def application_pages(
        self, locator: HaringeyLocatorV1
    ) -> HaringeyApplicationPagesV1:
        """Open detail, comments, and files without activating downloads."""
        return await self.run_browser(
            lambda page: self._application_pages(page, locator)
        )

    async def _validated_page(
        self, page: Page, page_number: int
    ) -> HaringeySearchPageV1:
        if page_number < 1:
            raise HaringeyPageObjectPaginationError(page_number)
        await page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")
        await page.get_by_role("button", name=_QUICK_LINK_BUTTON, exact=True).click()
        await page.wait_for_url("**/pr/s/register-view**")
        query = parse_qs(urlsplit(page.url).query)
        if query.get("c__r") != [REGISTER_NAME] or not query.get("c__q"):
            raise HaringeyPageObjectRouteError
        encoded_query = query["c__q"][0]
        initial_range = _parse_result_range(
            await page.locator(_RESULT_COUNT).inner_text()
        )
        page_count = max(1, ceil(initial_range.total / _PAGE_SIZE))
        if page_number > page_count:
            raise HaringeyPageObjectPaginationError(page_number)
        if page_number > 1:
            await _select_page(page, page_number)
        result_range = _parse_result_range(
            await page.locator(_RESULT_COUNT).inner_text()
        )
        _assert_range(result_range, page_number)
        cards = page.locator(_RESULT_CARD)
        card_count = await cards.count()
        if card_count != result_range.visible_count:
            raise HaringeyPageObjectCountError(result_range.visible_count, card_count)
        hits = []
        for index in range(card_count):
            card = cards.nth(index)
            values = _parse_card_text(await card.inner_text())
            link = card.locator(_RESULT_LINK).first
            href = await link.get_attribute("href")
            if not href:
                _raise_page_parse("detail link")
            record_id = _record_id(href)
            hits.append(
                HaringeySearchHitV1(
                    record_id=record_id,
                    public_reference=_card_field(values, "Application Reference"),
                    address=_card_field(values, "Site Address"),
                    proposal=_card_field(values, "Proposal"),
                    valid_date=_parse_card_date(_card_field(values, "Date Valid")),
                    status=_card_field(values, "Application Status"),
                    detail_url=HttpUrl(urljoin(page.url, href)),
                )
            )
        body = (await page.content()).encode()
        evidence = self.retain_rendered(page.url, body)
        return HaringeySearchPageV1(
            register_name=REGISTER_NAME,
            quick_link_name=QUICK_LINK_NAME,
            encoded_query=encoded_query,
            page_number=page_number,
            reported_page_count=page_count,
            reported_result_count=result_range.total,
            hits=tuple(hits),
            evidence=evidence,
        )

    async def _application_pages(
        self, page: Page, locator: HaringeyLocatorV1
    ) -> HaringeyApplicationPagesV1:
        await page.goto(str(locator.detail_url), wait_until="domcontentloaded")
        await page.get_by_role(
            "heading", name=locator.public_reference, exact=True
        ).wait_for()
        detail_body = (await page.content()).encode()
        detail = self.retain_rendered(page.url, detail_body)

        await page.get_by_role("tab", name="Comments", exact=True).click()
        comments_body = (await page.content()).encode()
        comments = self.retain_rendered(page.url, comments_body)

        await page.get_by_role("tab", name="Files", exact=True).click()
        table = page.get_by_role("table")
        await table.wait_for()
        rows = await table.get_by_role("row").count()
        if rows < 1:
            _raise_page_parse("files header row")
        files_body = (await page.content()).encode()
        files = self.retain_rendered(page.url, files_body)
        return HaringeyApplicationPagesV1(
            detail=detail,
            comments=comments,
            files=files,
            reported_file_count=rows - 1,
        )


@dataclass(frozen=True, slots=True)
class _ResultRange:
    """Reported inclusive range for one public result page."""

    start: int
    end: int
    total: int

    @property
    def visible_count(self) -> int:
        """Return the number of rows claimed for this page."""
        return max(0, self.end - self.start + 1)


async def _select_page(page: Page, page_number: int) -> None:
    selector = f'a.pr-pagination__link[data-id="{page_number}"]'
    target = page.locator(selector)
    page_sets = 0
    while await target.count() == 0:
        next_set = page.get_by_role("link", name="Nextset of pages", exact=True)
        if await next_set.count() == 0 or page_sets == _MAX_PAGE_SETS:
            raise HaringeyPageObjectPaginationError(page_number)
        await next_set.click()
        page_sets += 1
    await target.click()
    expected_start = (page_number - 1) * _PAGE_SIZE + 1
    await page.wait_for_function(
        "([selector, start]) => document.querySelector(selector)?.textContent"
        ".includes(`Showing ${start} to`)",
        arg=[_RESULT_COUNT, expected_start],
    )


def _parse_result_range(value: str) -> _ResultRange:
    match = re.search(
        r"Showing\s+(\d+)\s+to\s+(\d+)\s+of\s+(\d+)\s+results",
        value,
        re.IGNORECASE,
    )
    if match is None:
        _raise_page_parse("reported result range")
    start, end, total = (int(part) for part in match.groups())
    if start < 1 or end < start or end > total:
        _raise_page_parse("valid result range")
    return _ResultRange(start, end, total)


def _assert_range(result_range: _ResultRange, page_number: int) -> None:
    expected_start = (page_number - 1) * _PAGE_SIZE + 1
    expected_end = min(page_number * _PAGE_SIZE, result_range.total)
    if (result_range.start, result_range.end) != (expected_start, expected_end):
        raise HaringeyPageObjectPaginationError(page_number)


def _parse_card_text(value: str) -> dict[str, str]:
    lines = tuple(line.strip() for line in value.splitlines() if line.strip())
    fields: dict[str, str] = {}
    labels = {
        "Application Reference",
        "Site Address",
        "Proposal",
        "Date Valid",
        "Application Status",
    }
    for index, line in enumerate(lines):
        label = line.rstrip(":")
        if label in labels and index + 1 < len(lines):
            fields[label] = lines[index + 1]
    for label in labels:
        _card_field(fields, label)
    return fields


def _card_field(fields: dict[str, str], label: str) -> str:
    value = fields.get(label)
    if not value:
        _raise_page_parse(label)
    return value


def _parse_card_date(value: str) -> date:
    try:
        return _parse_date(value)
    except ValueError as error:
        field = "Date Valid"
        raise HaringeyPageObjectParseError(field) from error


def _record_id(href: str) -> str:
    match = re.search(r"/pr/s/detail/([^/?]+)", href)
    if match is None:
        _raise_page_parse("Salesforce record id")
    return match.group(1)


class HaringeyPageObjectParseError(ValueError):
    """Rendered Haringey content violated the captured selector contract."""

    def __init__(self, field: str) -> None:
        """Name the safe parser field."""
        super().__init__(f"missing Haringey page-object field {field}")


class HaringeyPageObjectRouteError(ValueError):
    """The quick link did not reach the recorded Arcus register route."""


class HaringeyPageObjectPaginationError(ValueError):
    """A requested or rendered page was outside the reported result set."""

    def __init__(self, page_number: int) -> None:
        """Identify only the public page number."""
        super().__init__(f"Haringey result page {page_number} is unavailable")


class HaringeyPageObjectCountError(ValueError):
    """Visible cards did not match the reported range."""

    def __init__(self, expected: int, observed: int) -> None:
        """Expose aggregate counts only."""
        super().__init__(f"Haringey page reported {expected} rows, observed {observed}")


def _raise_page_parse(field: str) -> NoReturn:
    raise HaringeyPageObjectParseError(field)

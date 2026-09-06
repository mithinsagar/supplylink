"""REST source adapter.

A network source fails in ways a file does not, so this adapter is mostly about
failing well.

**Retries are selective.** Only the statuses a contract lists as transient are
retried, and the contract model refuses to let 4xx statuses onto that list.
Retrying a 401 four times does not fix the credential; it turns an instant,
clear error into a slow, confusing one and hammers the vendor while doing it.
Connection errors and timeouts are always retried — those are transient by
definition.

**Backoff is exponential with jitter.** Full jitter, because a fleet of clients
retrying on the same doubling schedule reconverges into synchronised bursts and
keeps a recovering service down.

**Pagination has a hard stop.** A cursor that never advances, or a page counter
the server always answers, would otherwise loop forever. ``max_pages`` bounds it
and exceeding the bound is an error, not a silent truncation — quietly loading
half a catalogue is worse than loading none.

**Secrets are never logged.** The token comes from an environment variable named
by the contract, is attached to the request, and appears in no log line, error
message or metric.
"""

from __future__ import annotations

import os
import random
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx

from supplylink.errors import IngestionError, SourceUnavailableError
from supplylink.ingestion.base import ExtractionResult, SourceAdapter, SourceRecord
from supplylink.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

    from supplylink.contracts.model import Contract, RestSourceConfig

log = get_logger(__name__)


class RestSourceAdapter(SourceAdapter):
    """Reads a paginated JSON catalogue over HTTP."""

    def __init__(self, contract: Contract, client: httpx.Client | None = None) -> None:
        super().__init__(contract)
        if contract.source.rest is None:  # pragma: no cover - contract model guarantees this
            raise IngestionError(f"contract {contract.source_id} has no rest configuration")
        self.config: RestSourceConfig = contract.source.rest
        # Injected for tests, which drive a mock transport rather than a socket.
        self._client = client
        self._owns_client = client is None
        self._attempts_spent = 0

    # -- extraction -------------------------------------------------------

    def extract(self) -> ExtractionResult:
        records = list(self.iter_records())
        return ExtractionResult(
            records=records,
            origin=urljoin(self.config.base_url, self.config.path),
            metrics={
                "pages": self._pages_fetched,
                "http_attempts": self._attempts_spent,
                "retries": max(0, self._attempts_spent - self._pages_fetched),
            },
        )

    def iter_records(self) -> Iterator[SourceRecord]:
        self._pages_fetched = 0
        self._attempts_spent = 0
        client = self._client or self._build_client()
        # Resolved once per extraction and passed on every request, so an
        # injected client cannot end up unauthenticated.
        headers = self._headers()
        try:
            ordinal = 0
            for page in self._iter_pages(client, headers):
                for payload in self._extract_records(page):
                    yield SourceRecord(ordinal=ordinal, payload=payload)
                    ordinal += 1
        finally:
            if self._owns_client:
                client.close()

    # -- transport --------------------------------------------------------

    def _build_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.config.base_url,
            timeout=httpx.Timeout(self.config.timeout_seconds),
            follow_redirects=True,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", **self.config.headers}
        auth = self.config.auth

        if auth.type == "none":
            return headers
        if auth.token_env is None:
            raise IngestionError(
                f"source {self.source_id}: auth.type is {auth.type!r} but no token_env was named"
            )

        token = os.environ.get(auth.token_env)
        if not token:
            raise IngestionError(
                f"source {self.source_id}: environment variable {auth.token_env} is not set. "
                f"The credential is read from the environment and never stored in the contract."
            )

        headers[auth.header_name] = f"Bearer {token}" if auth.type == "bearer" else token
        return headers

    # -- pagination -------------------------------------------------------

    def _iter_pages(
        self, client: httpx.Client, headers: dict[str, str]
    ) -> Iterator[dict[str, Any] | list[Any]]:
        pagination = self.config.pagination

        if pagination.style == "none":
            yield self._request(client, dict(self.config.query), headers)
            self._pages_fetched = 1
            return

        if pagination.style == "page_number":
            yield from self._iter_numbered_pages(client, headers)
            return

        yield from self._iter_cursor_pages(client, headers)

    def _iter_numbered_pages(
        self, client: httpx.Client, headers: dict[str, str]
    ) -> Iterator[dict[str, Any] | list[Any]]:
        pagination = self.config.pagination
        page = pagination.first_page
        total_pages: int | None = None

        while True:
            params = {
                **self.config.query,
                pagination.page_param: str(page),
                pagination.size_param: str(pagination.page_size),
            }
            body = self._request(client, params, headers)
            self._pages_fetched += 1
            yield body

            if total_pages is None and pagination.total_pages_path:
                total_pages = _coerce_int(_dig(body, pagination.total_pages_path))

            page += 1
            fetched = page - pagination.first_page

            if total_pages is not None:
                if fetched >= total_pages:
                    return
            elif not self._extract_records(body):
                # No page count advertised: stop at the first empty page.
                return

            self._guard_page_limit(fetched)

    def _iter_cursor_pages(
        self, client: httpx.Client, headers: dict[str, str]
    ) -> Iterator[dict[str, Any] | list[Any]]:
        pagination = self.config.pagination
        if not pagination.next_cursor_path:
            raise IngestionError(
                f"source {self.source_id}: cursor pagination requires next_cursor_path"
            )

        cursor: str | None = None
        seen: set[str] = set()

        while True:
            params = dict(self.config.query)
            if cursor is not None:
                params[pagination.cursor_param] = cursor
            body = self._request(client, params, headers)
            self._pages_fetched += 1
            yield body

            next_cursor = _dig(body, pagination.next_cursor_path)
            if next_cursor in (None, ""):
                return

            cursor = str(next_cursor)
            if cursor in seen:
                # A server that keeps handing back a cursor it already gave us
                # would loop forever. Stop and say so.
                raise IngestionError(
                    f"source {self.source_id}: pagination cursor repeated after "
                    f"{self._pages_fetched} pages; the feed is not advancing"
                )
            seen.add(cursor)
            self._guard_page_limit(self._pages_fetched)

    def _guard_page_limit(self, fetched: int) -> None:
        if fetched >= self.config.pagination.max_pages:
            raise IngestionError(
                f"source {self.source_id}: stopped after {fetched} pages "
                f"(pagination.max_pages). Loading a truncated catalogue would look "
                f"like a mass deletion downstream."
            )

    # -- one request, with retries ---------------------------------------

    def _request(
        self, client: httpx.Client, params: dict[str, str], headers: dict[str, str]
    ) -> dict[str, Any] | list[Any]:
        """Fetch one page, retrying only what is worth retrying."""
        last_error: str = "unknown"

        for attempt in range(1, self.config.max_attempts + 1):
            self._attempts_spent += 1
            try:
                response = client.request(
                    self.config.method,
                    self.config.path,
                    params=params,
                    headers=headers,
                    timeout=self.config.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if not self._sleep_before_retry(attempt, last_error):
                    raise SourceUnavailableError(
                        f"source {self.source_id} unreachable after {attempt} "
                        f"attempts: {last_error}"
                    ) from exc
                continue

            if response.status_code in self.config.retry_on_status:
                last_error = f"HTTP {response.status_code}"
                if not self._sleep_before_retry(attempt, last_error, response=response):
                    raise SourceUnavailableError(
                        f"source {self.source_id} returned {last_error} on all {attempt} attempts"
                    )
                continue

            if response.is_error:
                # Not on the retry list, so it will not improve. Fail immediately
                # with the body truncated — a vendor error page can be a megabyte.
                raise IngestionError(
                    f"source {self.source_id} returned HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )

            return self._parse(response)

        raise SourceUnavailableError(  # pragma: no cover - loop always returns or raises
            f"source {self.source_id} exhausted retries: {last_error}"
        )

    def _sleep_before_retry(
        self, attempt: int, reason: str, response: httpx.Response | None = None
    ) -> bool:
        """Wait before the next attempt. Returns False when attempts are exhausted."""
        if attempt >= self.config.max_attempts:
            return False

        delay = self._retry_after(response)
        if delay is None:
            # Full jitter: exponential ceiling, uniform draw beneath it. Keeps a
            # fleet of clients from resynchronising into retry storms.
            ceiling = min(
                self.config.backoff_initial_seconds * (2 ** (attempt - 1)),
                self.config.backoff_max_seconds,
            )
            delay = random.uniform(0, ceiling)  # noqa: S311 - jitter, not cryptography

        log.warning(
            "retrying source request",
            extra={
                "source_id": self.source_id,
                "stage": "ingestion",
                "attempt": attempt,
                "max_attempts": self.config.max_attempts,
                "reason": reason,
                "sleep_seconds": round(delay, 3),
            },
        )
        time.sleep(delay)
        return True

    def _retry_after(self, response: httpx.Response | None) -> float | None:
        """Honour a server's ``Retry-After`` when it sends one.

        The server knows when it will be ready and we do not. Capped at the
        contract's maximum so a hostile or broken header cannot stall the run.
        """
        if response is None:
            return None
        header = response.headers.get("Retry-After")
        if not header:
            return None
        try:
            return min(float(header), self.config.backoff_max_seconds)
        except ValueError:
            return None  # HTTP-date form; not worth parsing for this purpose

    # -- response shaping -------------------------------------------------

    def _parse(self, response: httpx.Response) -> dict[str, Any] | list[Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise IngestionError(
                f"source {self.source_id} returned a non-JSON body "
                f"(content-type {response.headers.get('content-type', 'unset')!r})"
            ) from exc
        if not isinstance(body, dict | list):
            raise IngestionError(
                f"source {self.source_id} returned JSON {type(body).__name__}, "
                f"expected an object or an array"
            )
        return body

    def _extract_records(self, body: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
        """Pull the record array out of a response body."""
        payload: Any = body
        if self.config.records_path:
            payload = _dig(body, self.config.records_path)
            if payload is None:
                raise IngestionError(
                    f"source {self.source_id}: no value at records_path "
                    f"{self.config.records_path!r} in the response"
                )

        if not isinstance(payload, list):
            raise IngestionError(
                f"source {self.source_id}: expected a list of records at "
                f"{self.config.records_path or '<root>'}, got {type(payload).__name__}"
            )

        non_objects = sum(1 for item in payload if not isinstance(item, dict))
        if non_objects:
            raise IngestionError(
                f"source {self.source_id}: {non_objects} of {len(payload)} records are "
                f"not JSON objects"
            )
        return payload


def _dig(body: Any, path: str) -> Any:
    """Follow a dotted path into a nested structure, or return None."""
    current = body
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

"""Source adapters: file handling, HTTP failure modes, retries and pagination.

The REST tests drive a mock transport rather than a live server, so retry and
backoff behaviour is asserted deterministically and the suite needs no network.
Backoff sleeps are patched out — the timing is not what is under test, the
decision to retry is.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from supplylink.contracts.loader import load_contract, parse_contract
from supplylink.errors import IngestionError, SourceUnavailableError
from supplylink.ingestion.csv_adapter import RAGGED_MARKER, CsvSourceAdapter
from supplylink.ingestion.rest_adapter import RestSourceAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("supplylink.ingestion.rest_adapter.time.sleep", lambda _seconds: None)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def csv_contract(path_glob: str, data_dir: Path) -> object:
    contract = load_contract(REPO_ROOT / "contracts" / "erp.yaml")
    object.__setattr__(contract.source.csv, "path_glob", path_glob)
    return contract


class TestCsvAdapter:
    def test_reads_a_well_formed_file(self, isolated_data_dir, write_erp_feed) -> None:
        from tests.integration.feeds import GOOD_ROWS

        write_erp_feed(GOOD_ROWS)
        result = CsvSourceAdapter(csv_contract("samples/erp/*.csv", isolated_data_dir)).extract()

        assert result.count == 3
        assert [record.ordinal for record in result.records] == [0, 1, 2]
        assert result.records[0].payload["SKU"] == "WM-001"

    def test_files_are_processed_in_a_stable_order(self, isolated_data_dir) -> None:
        """Filesystem iteration order is not a guarantee on any platform."""
        landing = isolated_data_dir / "samples" / "erp"
        header = "SKU,Name,Category,Price,Currency,Stock,Supplier,Status\n"
        (landing / "b_second.csv").write_text(
            header + "B-001,B,Electronics,1,INR,1,SUP-042,ACTIVE\n"
        )
        (landing / "a_first.csv").write_text(
            header + "A-001,A,Electronics,1,INR,1,SUP-042,ACTIVE\n"
        )

        result = CsvSourceAdapter(csv_contract("samples/erp/*.csv", isolated_data_dir)).extract()
        assert [r.payload["SKU"] for r in result.records] == ["A-001", "B-001"]

    def test_a_missing_file_fails_the_run(self, isolated_data_dir) -> None:
        with pytest.raises(IngestionError, match="no files matched"):
            CsvSourceAdapter(csv_contract("samples/nothing/*.csv", isolated_data_dir)).extract()

    def test_a_missing_header_fails_the_run(self, isolated_data_dir) -> None:
        """Loading it would null out a whole canonical column."""
        (isolated_data_dir / "samples" / "erp" / "x.csv").write_text("SKU,Name\nA,B\n")
        with pytest.raises(IngestionError, match="missing required headers"):
            CsvSourceAdapter(csv_contract("samples/erp/*.csv", isolated_data_dir)).extract()

    def test_an_empty_file_fails_the_run(self, isolated_data_dir) -> None:
        (isolated_data_dir / "samples" / "erp" / "x.csv").write_text("")
        with pytest.raises(IngestionError, match="no header row"):
            CsvSourceAdapter(csv_contract("samples/erp/*.csv", isolated_data_dir)).extract()

    def test_a_wrong_encoding_fails_the_run_with_a_byte_offset(self, isolated_data_dir) -> None:
        """Guessing an alternative encoding would put mojibake in the warehouse."""
        (isolated_data_dir / "samples" / "erp" / "x.csv").write_bytes(
            b"SKU,Name,Category,Price,Currency,Stock,Supplier,Status\n"
            b"A-001,Mo\xfcse,Electronics,1,INR,1,SUP-042,ACTIVE\n"
        )
        with pytest.raises(IngestionError, match="not valid utf-8"):
            CsvSourceAdapter(csv_contract("samples/erp/*.csv", isolated_data_dir)).extract()

    def test_duplicate_headers_fail_the_run(self, isolated_data_dir) -> None:
        (isolated_data_dir / "samples" / "erp" / "x.csv").write_text(
            "SKU,Name,Category,Price,Currency,Stock,Supplier,Status,SKU\nA,B,C,1,INR,1,S,ACTIVE,D\n"
        )
        with pytest.raises(IngestionError, match="duplicate headers"):
            CsvSourceAdapter(csv_contract("samples/erp/*.csv", isolated_data_dir)).extract()

    def test_a_ragged_row_is_a_record_problem_not_a_run_problem(self, isolated_data_dir) -> None:
        """Dropping it would make the record vanish with no trace."""
        (isolated_data_dir / "samples" / "erp" / "x.csv").write_text(
            "SKU,Name,Category,Price,Currency,Stock,Supplier,Status\n"
            "A-001,A,Electronics,1,INR,1,SUP-042,ACTIVE\n"
            "B-001,B,Electronics\n"
        )
        result = CsvSourceAdapter(csv_contract("samples/erp/*.csv", isolated_data_dir)).extract()

        assert result.count == 2
        assert result.metrics["ragged_rows"] == 1
        assert RAGGED_MARKER in result.records[1].payload

    def test_a_headers_only_file_yields_no_records(self, isolated_data_dir) -> None:
        (isolated_data_dir / "samples" / "erp" / "x.csv").write_text(
            "SKU,Name,Category,Price,Currency,Stock,Supplier,Status\n"
        )
        adapter = CsvSourceAdapter(csv_contract("samples/erp/*.csv", isolated_data_dir))
        assert adapter.extract().count == 0


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

REST_CONTRACT = """
contract:
  version: "1.0.0"
  effective_from: 2026-01-01
source:
  id: vendor_api
  display_name: Vendor API
  type: rest
  precedence: 30
  rest:
    base_url: https://vendor.test
    path: /v1/catalog
    records_path: items
    max_attempts: 3
    backoff_initial_seconds: 0.01
    pagination:
      style: page_number
      page_size: 2
      total_pages_path: meta.totalPages
      max_pages: 5
identity:
  source_record_id: itemId
mapping:
  canonical_sku:
    from: itemId
    transforms: [sku_normalise]
  product_name:
    from: description
  category:
    from: dept
  supplier_id:
    from: vendor
    transforms: [supplier_id_normalise]
  unit_price:
    from: price
  currency:
    from: ccy
    default: INR
  quantity_available:
    from: units
  status:
    constant: ACTIVE
reconciliation:
  checksum_columns: [canonical_sku, unit_price]
scd:
  tracked_attributes: [product_name, unit_price]
"""


def rest_adapter(handler) -> RestSourceAdapter:
    contract = parse_contract(REST_CONTRACT, path="<test>")
    client = httpx.Client(
        base_url=contract.source.rest.base_url,
        transport=httpx.MockTransport(handler),
    )
    return RestSourceAdapter(contract, client=client)


def item(index: int) -> dict[str, object]:
    return {
        "itemId": f"sku_{index:03d}",
        "description": f"Item {index}",
        "dept": "ELEC",
        "price": 100 + index,
        "units": 5,
        "vendor": "SUP042",
    }


class TestRestAdapter:
    def test_paginates_until_the_advertised_total(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            return httpx.Response(
                200, json={"items": [item(page * 2 - 1), item(page * 2)], "meta": {"totalPages": 3}}
            )

        result = rest_adapter(handler).extract()
        assert result.count == 6
        assert result.metrics["pages"] == 3
        assert [r.ordinal for r in result.records] == list(range(6))

    def test_retries_a_transient_failure_and_recovers(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"items": [item(1)], "meta": {"totalPages": 1}})

        result = rest_adapter(handler).extract()
        assert result.count == 1
        assert result.metrics["retries"] == 1

    def test_does_not_retry_an_authentication_failure(self) -> None:
        """Retrying a 401 turns a clear instant error into a slow confusing one."""
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(401, json={"detail": "bad token"})

        with pytest.raises(IngestionError, match="HTTP 401"):
            rest_adapter(handler).extract()
        assert attempts["n"] == 1, "a permanent failure must not be retried"

    def test_does_not_retry_a_not_found(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(404)

        with pytest.raises(IngestionError, match="HTTP 404"):
            rest_adapter(handler).extract()
        assert attempts["n"] == 1

    def test_exhausts_retries_then_fails_the_run(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(503)

        with pytest.raises(SourceUnavailableError, match="503"):
            rest_adapter(handler).extract()
        assert attempts["n"] == 3, "should stop at max_attempts"

    def test_a_connection_error_is_retried(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json={"items": [item(1)], "meta": {"totalPages": 1}})

        assert rest_adapter(handler).extract().count == 1
        assert attempts["n"] == 3

    def test_a_persistent_timeout_fails_the_run(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        with pytest.raises(SourceUnavailableError, match="unreachable"):
            rest_adapter(handler).extract()

    def test_a_non_json_body_fails_the_run(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>maintenance</html>")

        with pytest.raises(IngestionError, match="non-JSON body"):
            rest_adapter(handler).extract()

    def test_a_missing_records_path_fails_the_run(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [], "meta": {"totalPages": 1}})

        with pytest.raises(IngestionError, match="no value at records_path"):
            rest_adapter(handler).extract()

    def test_non_object_records_fail_the_run(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [1, 2], "meta": {"totalPages": 1}})

        with pytest.raises(IngestionError, match="not JSON objects"):
            rest_adapter(handler).extract()

    def test_page_limit_is_enforced(self) -> None:
        """Quietly loading half a catalogue looks like a mass deletion downstream."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [item(1)], "meta": {"totalPages": 999}})

        with pytest.raises(IngestionError, match="max_pages"):
            rest_adapter(handler).extract()

    def test_retry_after_header_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The server knows when it will be ready; we do not."""
        slept: list[float] = []
        monkeypatch.setattr("supplylink.ingestion.rest_adapter.time.sleep", slept.append)
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, json={"items": [item(1)], "meta": {"totalPages": 1}})

        rest_adapter(handler).extract()
        assert slept == [2.0]

    def test_a_hostile_retry_after_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []
        monkeypatch.setattr("supplylink.ingestion.rest_adapter.time.sleep", slept.append)
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503, headers={"Retry-After": "99999"})
            return httpx.Response(200, json={"items": [item(1)], "meta": {"totalPages": 1}})

        rest_adapter(handler).extract()
        assert slept == [8.0], "capped at backoff_max_seconds"

    def test_a_negative_retry_after_falls_back_to_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``time.sleep`` rejects a negative delay, so the header cannot reach it.

        RFC 9110 requires a non-negative delay-seconds. A server sending ``-1``
        is malformed, and passing that straight through turned a retryable 503
        into an unhandled ValueError part-way through a run.
        """
        slept: list[float] = []
        monkeypatch.setattr("supplylink.ingestion.rest_adapter.time.sleep", slept.append)
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503, headers={"Retry-After": "-1"})
            return httpx.Response(200, json={"items": [item(1)], "meta": {"totalPages": 1}})

        rest_adapter(handler).extract()
        assert slept, "the 503 should still have been retried"
        assert all(delay >= 0 for delay in slept), f"time.sleep would reject {slept}"


class TestRestCredentials:
    def test_a_missing_credential_names_the_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VENDOR_API_TOKEN", raising=False)
        contract = load_contract(REPO_ROOT / "contracts" / "suppliers" / "globalparts.yaml")
        with pytest.raises(IngestionError, match="VENDOR_API_TOKEN is not set"):
            RestSourceAdapter(contract).extract()

    def test_the_token_never_appears_in_the_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VENDOR_API_TOKEN", "super-secret-value")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error")

        contract = load_contract(REPO_ROOT / "contracts" / "suppliers" / "globalparts.yaml")
        object.__setattr__(contract.source.rest, "max_attempts", 1)
        client = httpx.Client(
            base_url=contract.source.rest.base_url, transport=httpx.MockTransport(handler)
        )
        with pytest.raises(SourceUnavailableError) as caught:
            RestSourceAdapter(contract, client=client).extract()
        assert "super-secret-value" not in str(caught.value)

"""Contract parsing and the cross-field checks that make a bad contract fail early."""

from __future__ import annotations

import pytest

from supplylink.contracts.loader import ContractRegistry, parse_contract, substitute_env
from supplylink.errors import ContractError

MINIMAL = """
contract:
  version: "1.0.0"
  effective_from: 2026-01-01
source:
  id: test_source
  display_name: Test Source
  type: csv
  precedence: 50
  csv:
    path_glob: samples/test/*.csv
identity:
  source_record_id: code
mapping:
  canonical_sku:
    from: code
    transforms: [trim, sku_normalise]
  product_name:
    from: name
  category:
    from: cat
  supplier_id:
    from: sup
    transforms: [supplier_id_normalise]
  unit_price:
    from: price
  currency:
    from: ccy
    default: INR
  quantity_available:
    from: qty
  status:
    constant: ACTIVE
validation:
  canonical_sku:
    required: true
    unique: true
reconciliation:
  checksum_columns: [canonical_sku, unit_price]
scd:
  tracked_attributes: [product_name, unit_price]
"""


def parse(text: str = MINIMAL) -> object:
    return parse_contract(text, path="<test>")


class TestValidContract:
    def test_parses(self) -> None:
        contract = parse()
        assert contract.source_id == "test_source"
        assert contract.version == "1.0.0"
        assert contract.source_record_id_field == "code"

    def test_content_hash_is_stable_across_formatting(self) -> None:
        """Hashing the parsed structure, not the file text, so comments are free."""
        commented = MINIMAL.replace("contract:", "# a comment\ncontract:")
        reindented = MINIMAL.replace("  version:", "\n  version:")
        assert parse().content_hash == parse(commented).content_hash
        assert parse().content_hash == parse(reindented).content_hash

    def test_content_hash_changes_when_meaning_changes(self) -> None:
        changed = parse(MINIMAL.replace("precedence: 50", "precedence: 60"))
        assert parse().content_hash != changed.content_hash


class TestStrictness:
    def test_a_misspelled_rule_key_is_rejected(self) -> None:
        """A silently-ignored typo would disable a rule and let bad data through."""
        with pytest.raises(ContractError, match="Extra inputs are not permitted"):
            parse(MINIMAL.replace("    required: true", "    requred: true"))

    def test_unknown_top_level_section_is_rejected(self) -> None:
        with pytest.raises(ContractError):
            parse(MINIMAL + "\nextras:\n  foo: bar\n")

    def test_non_mapping_document_is_rejected(self) -> None:
        with pytest.raises(ContractError, match="mapping at the top level"):
            parse("- just\n- a list\n")

    def test_invalid_yaml_is_rejected(self) -> None:
        with pytest.raises(ContractError, match="not valid YAML"):
            parse("contract: [unclosed\n")


class TestCrossFieldChecks:
    def test_mapping_to_a_non_canonical_field(self) -> None:
        with pytest.raises(ContractError, match="not in the canonical model"):
            parse(MINIMAL.replace("  product_name:", "  prodcut_name:"))

    def test_business_key_must_be_mapped(self) -> None:
        without_key = (
            MINIMAL.replace(
                "  canonical_sku:\n    from: code\n    transforms: [trim, sku_normalise]\n", ""
            )
            .replace(
                "validation:\n  canonical_sku:\n    required: true\n    unique: true\n",
                "validation: {}\n",
            )
            .replace(
                "checksum_columns: [canonical_sku, unit_price]", "checksum_columns: [unit_price]"
            )
        )
        with pytest.raises(ContractError, match="business key"):
            parse(without_key)

    def test_unknown_transform(self) -> None:
        with pytest.raises(ContractError, match="unregistered transforms"):
            parse(MINIMAL.replace("sku_normalise]", "sku_normalize]"))

    def test_validation_on_an_unmapped_field(self) -> None:
        with pytest.raises(ContractError, match="never produces"):
            parse(
                MINIMAL.replace(
                    "validation:\n  canonical_sku:",
                    "validation:\n  status:\n    required: true\n  canonical_sku:",
                ).replace("  status:\n    constant: ACTIVE\n", "")
            )

    def test_declared_type_must_match_the_canonical_model(self) -> None:
        with pytest.raises(ContractError, match="cannot redefine"):
            parse(
                MINIMAL.replace(
                    "  canonical_sku:\n    required: true",
                    "  unit_price:\n    type: integer\n  canonical_sku:\n    required: true",
                )
            )

    def test_tracking_a_measure_is_refused(self) -> None:
        """Versioning quantity would open a dimension row per run and bury real changes."""
        with pytest.raises(ContractError, match="contains measures"):
            parse(
                MINIMAL.replace(
                    "tracked_attributes: [product_name, unit_price]",
                    "tracked_attributes: [product_name, quantity_available]",
                )
            )

    def test_tracking_an_unmapped_field_is_refused(self) -> None:
        unmapped = MINIMAL.replace("  status:\n    constant: ACTIVE\n", "").replace(
            "tracked_attributes: [product_name, unit_price]",
            "tracked_attributes: [product_name, status]",
        )
        with pytest.raises(ContractError, match="never maps"):
            parse(unmapped)

    def test_checksum_column_must_be_mapped(self) -> None:
        with pytest.raises(ContractError, match="unknown fields"):
            parse(
                MINIMAL.replace(
                    "checksum_columns: [canonical_sku, unit_price]",
                    "checksum_columns: [canonical_sku, not_a_field]",
                )
            )

    def test_duplicate_checksum_columns(self) -> None:
        with pytest.raises(ContractError, match="duplicates"):
            parse(
                MINIMAL.replace(
                    "checksum_columns: [canonical_sku, unit_price]",
                    "checksum_columns: [canonical_sku, canonical_sku]",
                )
            )

    def test_undefined_lookup(self) -> None:
        with pytest.raises(ContractError, match="which is not defined"):
            parse(MINIMAL.replace("    from: cat", "    from: cat\n    lookup: missing_map"))

    def test_unused_lookup(self) -> None:
        """Almost always a mapping that forgot to name it."""
        with pytest.raises(ContractError, match="never referenced"):
            parse(MINIMAL + "\nlookups:\n  orphan_map:\n    A: B\n")

    def test_unknown_threshold_key(self) -> None:
        with pytest.raises(ContractError, match="unknown keys"):
            parse(
                MINIMAL.replace(
                    "  checksum_columns:",
                    "  thresholds:\n    typo_rate_pct:\n      warn: 1\n      fail: 2\n"
                    "  checksum_columns:",
                )
            )

    def test_warn_above_fail_threshold(self) -> None:
        with pytest.raises(ContractError, match="must not exceed"):
            parse(
                MINIMAL.replace(
                    "  checksum_columns:",
                    "  thresholds:\n    duplicate_rate_pct:\n      warn: 9\n      fail: 5\n"
                    "  checksum_columns:",
                )
            )

    def test_disallowed_referential_target(self) -> None:
        with pytest.raises(ContractError, match="not a permitted referential-integrity target"):
            parse(
                MINIMAL.replace(
                    "  canonical_sku:\n    required: true",
                    "  supplier_id:\n    references:\n      table: meta.pipeline_runs\n"
                    "      column: run_id\n  canonical_sku:\n    required: true",
                )
            )

    def test_duplicate_rule_names(self) -> None:
        rules = (
            "rules:\n"
            '  - name: same_name\n    expr: "unit_price > 0"\n    message: a\n'
            '  - name: same_name\n    expr: "unit_price > 1"\n    message: b\n'
        )
        with pytest.raises(ContractError, match="duplicate business rule"):
            parse(MINIMAL + "\n" + rules)

    def test_rule_referencing_a_non_canonical_field(self) -> None:
        rules = 'rules:\n  - name: bad\n    expr: "made_up_field > 0"\n    message: m\n'
        with pytest.raises(ContractError, match="not canonical"):
            parse(MINIMAL + "\n" + rules)


class TestSourceBlocks:
    def test_csv_type_requires_a_csv_block(self) -> None:
        with pytest.raises(ContractError, match=r"no source\.csv block"):
            parse(MINIMAL.replace("  csv:\n    path_glob: samples/test/*.csv\n", ""))

    def test_a_rest_contract_may_not_retry_permanent_failures(self) -> None:
        """Retrying a 401 four times turns a clear error into a slow, confusing one."""
        rest = MINIMAL.replace(
            "  type: csv\n  precedence: 50\n  csv:\n    path_glob: samples/test/*.csv",
            "  type: rest\n  precedence: 50\n  rest:\n    base_url: http://x\n"
            "    retry_on_status: [500, 401]",
        )
        with pytest.raises(ContractError, match="permanently-failing"):
            parse(rest)


class TestEnvironmentSubstitution:
    def test_default_is_used_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPPLYLINK_TEST_VAR", raising=False)
        assert substitute_env("${SUPPLYLINK_TEST_VAR:-fallback}", source="t") == "fallback"

    def test_environment_wins_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPPLYLINK_TEST_VAR", "real")
        assert substitute_env("${SUPPLYLINK_TEST_VAR:-fallback}", source="t") == "real"

    def test_missing_variable_without_default_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty base URL fails much later and much more confusingly."""
        monkeypatch.delenv("SUPPLYLINK_TEST_VAR", raising=False)
        with pytest.raises(ContractError, match="not set"):
            substitute_env("${SUPPLYLINK_TEST_VAR}", source="t")

    def test_substitution_recurses_into_structures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPPLYLINK_TEST_VAR", "v")
        result = substitute_env({"a": ["${SUPPLYLINK_TEST_VAR}"]}, source="t")
        assert result == {"a": ["v"]}

    def test_a_literal_dollar_is_left_alone(self) -> None:
        assert substitute_env("costs $5", source="t") == "costs $5"


class TestRegistry:
    def test_discovers_the_shipped_contracts(self, contracts_dir) -> None:
        registry = ContractRegistry.discover(contracts_dir)
        assert set(registry.source_ids()) == {"erp_core", "acme_supplier", "globalparts_api"}

    def test_unknown_source_lists_the_known_ones(self, contracts_dir) -> None:
        registry = ContractRegistry.discover(contracts_dir)
        with pytest.raises(ContractError, match="Known sources"):
            registry.get("no_such_source")

    def test_missing_directory_is_an_error(self, tmp_path) -> None:
        with pytest.raises(ContractError, match="does not exist"):
            ContractRegistry.discover(tmp_path / "nope")

    def test_empty_directory_is_an_error(self, tmp_path) -> None:
        with pytest.raises(ContractError, match="no contract files"):
            ContractRegistry.discover(tmp_path)

    def test_duplicate_source_ids_are_refused(self, tmp_path) -> None:
        """Otherwise which file wins depends on directory ordering."""
        (tmp_path / "a.yaml").write_text(MINIMAL)
        (tmp_path / "b.yaml").write_text(MINIMAL)
        with pytest.raises(ContractError, match="declared by two contracts"):
            ContractRegistry.discover(tmp_path)


def test_every_shipped_contract_is_valid(contracts_dir) -> None:
    """The contracts in the repository must load. They are part of the product."""
    registry = ContractRegistry.discover(contracts_dir)
    for contract in registry.all():
        assert contract.reconciliation.checksum_columns
        assert contract.scd.tracked_attributes
        assert contract.content_hash

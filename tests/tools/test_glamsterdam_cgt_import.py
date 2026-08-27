"""Tests for the reproducible CGT runtime importer."""

import csv
from pathlib import Path

import pytest

from tools.glamsterdam_cgt_import import load_cgt_records
from tools.glamsterdam_exposure_scan import normalize_address


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = ["dataset", "id", "chain", "addr", "fp_runtime"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def test_deduplicates_assessment_rows(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "abc.rt.hex").write_text("6000", encoding="utf-8")
    consolidated = tmp_path / "consolidated.csv"
    rows = [
        {"dataset": "a", "id": "1", "chain": "main", "addr": "0x1", "fp_runtime": "abc"},
        {"dataset": "a", "id": "1", "chain": "main", "addr": "0x1", "fp_runtime": "abc"},
    ]
    _write_csv(consolidated, rows)

    records = load_cgt_records(consolidated, runtime)
    assert records == [
        {
            "address": normalize_address("0x1"),
            "bytecode": "0x6000",
            "runtime_hash": "abc",
            "chain": "main",
            "source": "CGT",
        }
    ]


def test_missing_runtime_is_omitted(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    consolidated = tmp_path / "consolidated.csv"
    _write_csv(
        consolidated,
        [{"dataset": "a", "id": "1", "chain": "main", "addr": "0x1", "fp_runtime": "missing"}],
    )
    assert load_cgt_records(consolidated, runtime) == []


def test_conflicting_runtime_for_same_address_fails(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "abc.rt.hex").write_text("6000", encoding="utf-8")
    (runtime / "def.rt.hex").write_text("6001", encoding="utf-8")
    consolidated = tmp_path / "consolidated.csv"
    _write_csv(
        consolidated,
        [
            {"dataset": "a", "id": "1", "chain": "main", "addr": "0x1", "fp_runtime": "abc"},
            {"dataset": "b", "id": "2", "chain": "main", "addr": "0x1", "fp_runtime": "def"},
        ],
    )
    with pytest.raises(ValueError, match="conflicting runtime fingerprints"):
        load_cgt_records(consolidated, runtime)


def test_non_mainnet_rows_are_excluded_by_default(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "abc.rt.hex").write_text("6000", encoding="utf-8")
    consolidated = tmp_path / "consolidated.csv"
    _write_csv(
        consolidated,
        [{"dataset": "a", "id": "1", "chain": "ropsten", "addr": "0x1", "fp_runtime": "abc"}],
    )
    assert load_cgt_records(consolidated, runtime) == []
    assert len(load_cgt_records(consolidated, runtime, mainnet_only=False)) == 1

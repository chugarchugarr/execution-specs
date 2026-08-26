"""
Executable phase-1 proof for Glamsterdam storage-write prepayment.

The proof reads the Osaka and Amsterdam gas constants directly from the
current execution-spec source tree. It must fail if those branch constants
move enough to invalidate either the liveness transition or conservation
claim.
"""

OSAKA_GAS_PATH = "src/ethereum/forks/osaka/vm/gas.py"
AMSTERDAM_GAS_PATH = "src/ethereum/forks/amsterdam/vm/gas.py"


def _uint_constant(path: str, name: str) -> int:
    """Read a direct ``Uint(...)`` gas constant from a fork gas module."""
    with open(path, encoding="utf-8") as source_file:
        for line in source_file:
            stripped = line.strip()
            if stripped.startswith(f"{name}:") and "Uint(" in stripped:
                raw_value = stripped.rsplit("Uint(", 1)[1].split(")", 1)[0]
                return int(raw_value)
    raise AssertionError(f"direct Uint constant not found: {path}:{name}")


def _amsterdam_access_list_formulas_survive() -> None:
    """Pin the access-list prepayment formulas used by the proof."""
    with open(AMSTERDAM_GAS_PATH, encoding="utf-8") as source_file:
        source = source_file.read()

    assert "TX_ACCESS_LIST_ADDRESS: Final[ExecutionGas] = (" in source
    assert "COLD_ACCOUNT_ACCESS - WARM_ACCESS" in source
    assert "TX_ACCESS_LIST_STORAGE_KEY: Final[ExecutionGas] = (" in source
    assert "COLD_STORAGE_ACCESS - WARM_ACCESS" in source


def test_osaka_pass_amsterdam_fail_prepaid_pass() -> None:
    """Prove one immutable child budget flips PASS -> FAIL -> PASS."""
    osaka_warm = _uint_constant(OSAKA_GAS_PATH, "WARM_ACCESS")
    osaka_cold_storage = _uint_constant(
        OSAKA_GAS_PATH, "COLD_STORAGE_ACCESS"
    )
    osaka_cold_write = _uint_constant(OSAKA_GAS_PATH, "COLD_STORAGE_WRITE")
    osaka_push = _uint_constant(OSAKA_GAS_PATH, "VERY_LOW")

    amsterdam_warm = _uint_constant(AMSTERDAM_GAS_PATH, "WARM_ACCESS")
    amsterdam_cold_storage = _uint_constant(
        AMSTERDAM_GAS_PATH, "COLD_STORAGE_ACCESS"
    )
    amsterdam_write = _uint_constant(AMSTERDAM_GAS_PATH, "STORAGE_WRITE")
    amsterdam_push = _uint_constant(AMSTERDAM_GAS_PATH, "VERY_LOW")

    legacy_write = osaka_cold_write - osaka_cold_storage - osaka_warm
    fixed_child_gas = osaka_cold_write + 2 * osaka_push

    osaka_required = osaka_cold_write + 2 * osaka_push
    amsterdam_required = (
        amsterdam_cold_storage + amsterdam_write + 2 * amsterdam_push
    )
    prepaid_required = amsterdam_warm + legacy_write + 2 * amsterdam_push

    assert legacy_write == 2_800
    assert fixed_child_gas == 5_006
    assert osaka_required == 5_006
    assert amsterdam_required == 12_106
    assert prepaid_required == 2_906

    assert osaka_required <= fixed_child_gas
    assert amsterdam_required > fixed_child_gas
    assert prepaid_required <= fixed_child_gas


def test_prepayment_preserves_glamsterdam_execution_resource_charge() -> None:
    """Prove repricing relocation preserves the full Amsterdam core charge."""
    _amsterdam_access_list_formulas_survive()

    osaka_warm = _uint_constant(OSAKA_GAS_PATH, "WARM_ACCESS")
    osaka_cold_storage = _uint_constant(
        OSAKA_GAS_PATH, "COLD_STORAGE_ACCESS"
    )
    osaka_cold_write = _uint_constant(OSAKA_GAS_PATH, "COLD_STORAGE_WRITE")

    amsterdam_warm = _uint_constant(AMSTERDAM_GAS_PATH, "WARM_ACCESS")
    amsterdam_cold_account = _uint_constant(
        AMSTERDAM_GAS_PATH, "COLD_ACCOUNT_ACCESS"
    )
    amsterdam_cold_storage = _uint_constant(
        AMSTERDAM_GAS_PATH, "COLD_STORAGE_ACCESS"
    )
    amsterdam_write = _uint_constant(AMSTERDAM_GAS_PATH, "STORAGE_WRITE")

    legacy_write = osaka_cold_write - osaka_cold_storage - osaka_warm
    write_repricing_delta = amsterdam_write - legacy_write
    access_list_address = amsterdam_cold_account - amsterdam_warm
    access_list_storage_key = amsterdam_cold_storage - amsterdam_warm

    baseline_call_access = amsterdam_cold_account
    baseline_storage = amsterdam_cold_storage + amsterdam_write
    baseline_core_charge = baseline_call_access + baseline_storage

    prepaid_call_access = access_list_address + amsterdam_warm
    prepaid_storage = (
        access_list_storage_key
        + amsterdam_warm
        + write_repricing_delta
        + legacy_write
    )
    prepaid_core_charge = prepaid_call_access + prepaid_storage

    assert write_repricing_delta == 7_200
    assert baseline_call_access == prepaid_call_access == 3_000
    assert baseline_storage == prepaid_storage == 12_100
    assert baseline_core_charge == prepaid_core_charge == 15_100


def test_prepayment_is_not_a_subsidy() -> None:
    """Prove the rescued local frame does not make global work cheaper."""
    osaka_warm = _uint_constant(OSAKA_GAS_PATH, "WARM_ACCESS")
    osaka_cold_storage = _uint_constant(
        OSAKA_GAS_PATH, "COLD_STORAGE_ACCESS"
    )
    osaka_cold_write = _uint_constant(OSAKA_GAS_PATH, "COLD_STORAGE_WRITE")

    amsterdam_warm = _uint_constant(AMSTERDAM_GAS_PATH, "WARM_ACCESS")
    amsterdam_cold_storage = _uint_constant(
        AMSTERDAM_GAS_PATH, "COLD_STORAGE_ACCESS"
    )
    amsterdam_write = _uint_constant(AMSTERDAM_GAS_PATH, "STORAGE_WRITE")

    legacy_write = osaka_cold_write - osaka_cold_storage - osaka_warm
    write_repricing_delta = amsterdam_write - legacy_write
    access_list_storage_key = amsterdam_cold_storage - amsterdam_warm

    local_prepaid_sstore = amsterdam_warm + legacy_write
    globally_paid_sstore = (
        access_list_storage_key
        + local_prepaid_sstore
        + write_repricing_delta
    )

    assert local_prepaid_sstore == 2_900
    assert globally_paid_sstore == amsterdam_cold_storage + amsterdam_write

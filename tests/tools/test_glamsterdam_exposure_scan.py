"""Tests for the conservative Glamsterdam mainnet exposure scanner."""

from tools.glamsterdam_exposure_scan import (
    classify,
    contains_sstore,
    decode_hex,
    normalize_address,
    recover_fixed_calls,
    summarize,
)


def _record(address: int, bytecode: str, value_usd: float | None = None):
    record = {
        "address": normalize_address(hex(address)),
        "bytecode": bytecode,
        "_code": decode_hex(bytecode),
    }
    if value_usd is not None:
        record["value_usd"] = value_usd
    return record


def _fixed_call(target: int, gas: int) -> str:
    # CALL stack, bottom -> top:
    # retSize, retOffset, argsSize, argsOffset, value, target, gas.
    target_hex = target.to_bytes(20, "big").hex()
    gas_hex = gas.to_bytes(2, "big").hex()
    return "0x" + "5f" * 5 + "73" + target_hex + "61" + gas_hex + "f100"


def test_push_immediate_55_is_not_sstore() -> None:
    assert contains_sstore(decode_hex("0x605500")) is False
    assert contains_sstore(decode_hex("0x600160005500")) is True


def test_oversized_shl_follows_evm_semantics_without_allocating() -> None:
    caller = normalize_address("0x100")
    huge_shift = "ff" * 32
    # PUSH1 1; PUSH32 (2**256-1); SHL; STOP. EIP-145 defines this as zero.
    code = decode_hex("0x60017f" + huge_shift + "1b00")
    assert recover_fixed_calls(caller, code) == []


def test_fixed_call_in_repricing_window_is_recovered() -> None:
    caller = normalize_address("0x101")
    calls = recover_fixed_calls(caller, decode_hex(_fixed_call(0x200, 5_000)))
    assert len(calls) == 1
    assert calls[0]["gas"] == 5_000
    assert calls[0]["target"] == normalize_address("0x200")
    assert calls[0]["value"] == 0


def test_amsterdam_precompile_is_not_sstore_exposure() -> None:
    records = [_record(0x101, _fixed_call(0x01, 3_000))]
    sites = classify(records)
    assert len(sites) == 1
    assert sites[0].window == "repricing_window"
    assert sites[0].target == normalize_address("0x01")
    assert sites[0].callee_has_sstore is False
    assert sites[0].classification == "direct_window_precompile"

    report = summarize(records, sites)
    assert report["precompile_direct_window_sites"] == 1
    assert report["unresolved_direct_window_sites"] == 0


def test_resolved_sstore_callee_is_static_high() -> None:
    records = [
        _record(0x101, _fixed_call(0x200, 5_000)),
        _record(0x200, "0x600160005500", value_usd=123.0),
    ]
    sites = classify(records)
    assert len(sites) == 1
    assert sites[0].window == "repricing_window"
    assert sites[0].callee_has_sstore is True
    assert sites[0].classification == "static_high"

    report = summarize(records, sites)
    assert report["static_high_sites"] == 1
    assert report["valued_static_high_callees"] == 1
    assert report["reported_static_high_value_usd"] == 123.0


def test_unresolved_target_never_becomes_high_confidence() -> None:
    records = [_record(0x101, _fixed_call(0x200, 5_000))]
    sites = classify(records)
    assert sites[0].classification == "direct_window_unresolved"
    assert sites[0].callee_has_sstore is None


def test_gas_below_osaka_floor_is_only_candidate() -> None:
    records = [
        _record(0x101, _fixed_call(0x200, 2_899)),
        _record(0x200, "0x600160005500"),
    ]
    sites = classify(records)
    assert sites[0].window == "below_osaka_floor"
    assert sites[0].classification == "fixed_gas_candidate"


def test_gas_at_amsterdam_requirement_is_outside_direct_window() -> None:
    records = [
        _record(0x101, _fixed_call(0x200, 10_100)),
        _record(0x200, "0x600160005500"),
    ]
    sites = classify(records)
    assert sites[0].window == "above_direct_window"
    assert sites[0].classification == "fixed_gas_candidate"


def test_value_is_deduplicated_by_resolved_callee() -> None:
    records = [
        _record(0x101, _fixed_call(0x300, 5_000)),
        _record(0x102, _fixed_call(0x300, 6_000)),
        _record(0x300, "0x600160005500", value_usd=1_000.0),
    ]
    sites = classify(records)
    report = summarize(records, sites)
    assert report["static_high_sites"] == 2
    assert report["valued_static_high_callees"] == 1
    assert report["reported_static_high_value_usd"] == 1_000.0

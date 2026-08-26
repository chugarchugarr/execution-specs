# Glamsterdam Write Prepayment — Phase 1

Base: `ethereum/execution-specs@20f7f6271a720091e5fea0a82e7bc802866ae36a` (`forks/amsterdam`, 2026-08-26).

## Decision gate

Do not change consensus semantics and do not scan mainnet until the current
execution-spec constants prove both invariants:

1. `Osaka PASS -> Amsterdam FAIL -> Amsterdam + prepayment PASS` under the
   same immutable child-call gas budget.
2. Moving the repricing delta outside the child preserves the full Amsterdam
   execution-resource charge.

The executable proof is:

`tests/amsterdam/eip8038_state_access_gas_cost_increase/test_write_prepayment_prototype.py`

This phase is deliberately a branch-pinned accounting/liveness proof. It does
**not** yet define a new transaction encoding or modify EVM consensus behavior.
That implementation is the next gate only if this proof survives CI.

## Minimal reproduction

Use an existing non-zero storage slot and a child that executes:

```text
PUSH value
PUSH key
SSTORE
```

Two `PUSH` instructions cost 6 execution gas. Give the child an immutable
5,006-gas budget.

### Osaka

For the first non-zero -> non-zero overwrite of a cold slot:

```text
2,100 cold storage access
+ 2,900 write component
+     6 stack setup
= 5,006
```

Result: `PASS`.

### Amsterdam, unchanged

Current `forks/amsterdam` constants:

```text
2,100 COLD_STORAGE_ACCESS
+ 10,000 STORAGE_WRITE
+      6 stack setup
= 12,106
```

The same immutable 5,006-gas child cannot execute it.

Result: `FAIL`.

### Amsterdam, write-prepaid

Decompose the Amsterdam write price:

```text
10,000 STORAGE_WRITE
= 2,800 historical write component
+ 7,200 repricing delta
```

Prepay the 7,200 delta outside the child and prewarm the storage key. The
child sees:

```text
100 warm access
+ 2,800 historical write component
+     6 stack setup
= 2,906
```

Result: `PASS` under the same 5,006-gas child budget.

## Conservation proof

The mechanism is invalid if it makes Amsterdam work cheaper globally.

Ordinary cold Amsterdam storage write:

```text
2,100 cold access + 10,000 write = 12,100
```

Prepaid split:

```text
2,000 access-list storage-key prepayment
+ 100 warm access in the child
+ 7,200 write-repricing prepayment
+ 2,800 historical write component in the child
= 12,100
```

Account access conserves independently:

```text
ordinary: 3,000 cold account access
prepaid: 2,900 access-list address prepayment + 100 warm runtime access
        = 3,000
```

Therefore the core state/call resource accounting is identical:

```text
ordinary Amsterdam core charge = 3,000 + 12,100 = 15,100
prepaid Amsterdam core charge  = 3,000 + 12,100 = 15,100
```

The proposal changes **where** the repricing delta is charged, not whether it
is charged.

## Next gate

Only after the phase-1 test passes:

1. choose an explicit opt-in transaction representation for write vouchers;
2. implement voucher state and rollback semantics in EELS;
3. prove change -> restore -> change net-metering, nested-call revert/halt
   behavior, duplicate voucher handling, and EIP-8037 state-gas interaction;
4. run the Amsterdam/Osaka test suites and independent client fixtures;
5. only then scan deployed mainnet bytecode for fixed-gas asset-release paths
   and quantify value reachable only through failing paths.

A mainnet value estimate before those gates would measure a hypothesis rather
than a demonstrated compatibility failure.

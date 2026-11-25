# Add Store.restock(sku, quantity) to src/inventory/store.py

## Summary

Add Store.restock, a receiving module that applies delivery notes, and tests.

That is the plan's approach. The code changed in 4 revision(s) since:

1. Removed the print() call: py.output.no-print is hard in this repository.
2. restock now raises ValueError for a quantity that is not positive.
3. Parse the quantity with int(), never eval().
4. receive checks every line before it applies any, with a test for it.

## Risks the plan named

The Planner named these before anything was built. No gate checks whether they were resolved, so judge each one before you approve.

- A delivery note with a malformed line.

## Task

> Add Store.restock(sku, quantity) to src/inventory/store.py: it adds quantity to an existing item's stock, raises ValueError for a quantity that is not positive, and raises KeyError for an unknown SKU. Add receive(store, note) in a new module src/inventory/receiving.py that applies a delivery note of "sku,quantity" lines with restock and returns how many lines it applied, and export it from the package.

## Changes (5 file(s))

- `src/inventory/__init__.py`
- `src/inventory/receiving.py`
- `src/inventory/store.py`
- `tests/acceptance/test_restocking.py`
- `tests/test_restock.py`

## Evidence

Every gate passed on exactly the tree delivered here, `6eed0d70e1e13d6e0f34cba1c062718634cb8bd2`.

| gate | result | report |
|---|---|---|
| Static analysis | passed | `sha256:ad897c91bc40f2bc2853ffea245c3766fa4db86b90a72bc2ed12a3a525cf8d6c` |
| Unit tests | passed | `sha256:1606a72271efe644da6954584de65466611287317af71e2b97663c62f7413abc` |
| Integration tests | passed, without network | `sha256:b26f9a0a26fba770ce7bc3d675fd3592e27f805731d8651b6eebaf1533769813` |
| Security review | passed | `sha256:1893770df2e7a846cb89bc46989f2f69453b7469f81bbbd7bc89713f0ee7f861` |

## Caught along the way

These checks failed earlier in this run. Each gate passed again on the tree delivered here (Evidence, above).

| gate | tree | what failed | report |
|---|---|---|---|
| Static analysis | `c6b58f2c7d36` | `preferences` | `sha256:070758e8bbb697dccf5af0aa018aed042b27aec946ef8b23bb32698e3b55b374` |
| Unit tests | `534d536b51ac` | impl_bug | `sha256:19bdb0a158ba2b319c9c762d9e6b5e42e6bb688d4f65e2d92cecd64a31b43350` |
| Security review | `4e54e7de7477` | 1 blocking finding(s) | `sha256:23df47e0a075df96e0ba9841a3c8bb6d28fe7ff8aaac7416096eea3113852074` |

## Review

1 finding(s) raised, 1 admitted.

- `c3-1` [major] receive() is not atomic: a bad line raises after the lines before it were applied, so a rejected delivery leaves the store half updated: accepted in negotiation

## Audit

- Run: `run-1` (journal: `msa inspect run-1`)
- Base commit: `71cd65d8b4802fc1c1a1a874272272480cf4c70b`
- Head tree: `6eed0d70e1e13d6e0f34cba1c062718634cb8bd2`
- Approve: `msa approve run-1`; send back: `msa approve run-1 --reject "why"`

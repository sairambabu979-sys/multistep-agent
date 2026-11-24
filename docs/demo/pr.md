# Add Store.restock(sku, quantity) to src/inventory/store.py

## Summary

Add Store.restock, a receiving module that applies delivery notes, and tests.

That is the plan's approach. The code changed in 4 revision(s) since:

1. Removed the print() call: py.output.no-print is hard in this repository.
2. restock now raises ValueError for a quantity that is not positive.
3. Parse the quantity with int(), never eval().
4. receive checks every line before it applies any, with a test for it.

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
| Static analysis | passed | `sha256:c092d44d177baa9aa9f43726d65260dc4984656b22954462253f454610630702` |
| Unit tests | passed | `sha256:674bed8743a13a6e90b1b62e41b72aa279397fc7b1c10d24e760146f9f1221fa` |
| Integration tests | passed, without network | `sha256:42456340313e0265facf3ff7f2acad9e04b1a02c88d172d4025a7a83279e09ca` |
| Security review | passed | `sha256:1893770df2e7a846cb89bc46989f2f69453b7469f81bbbd7bc89713f0ee7f861` |

## Caught along the way

These checks failed earlier in this run. Each gate passed again on the tree delivered here (Evidence, above).

| gate | tree | what failed | report |
|---|---|---|---|
| Static analysis | `c6b58f2c7d36` | `preferences` | `sha256:070758e8bbb697dccf5af0aa018aed042b27aec946ef8b23bb32698e3b55b374` |
| Unit tests | `534d536b51ac` | impl_bug | `sha256:61978a97348677f62920e0a0e68b17ffb9e6b1f42a85cc4411d5ae0a779506e7` |
| Security review | `4e54e7de7477` | 1 blocking finding(s) | `sha256:23df47e0a075df96e0ba9841a3c8bb6d28fe7ff8aaac7416096eea3113852074` |

## Review

1 finding(s) raised, 1 admitted.

- `c3-1` [major] receive() is not atomic: a bad line raises after the lines before it were applied, so a rejected delivery leaves the store half updated: accepted in negotiation

## Audit

- Run: `run-1` (journal: `msa inspect run-1`)
- Base commit: `4cecb5e50b3f77707ed83e6947917fee8e4118e4`
- Head tree: `6eed0d70e1e13d6e0f34cba1c062718634cb8bd2`
- Approve: `msa approve run-1`; send back: `msa approve run-1 --reject "why"`

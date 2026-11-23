# Add Store.restock(sku, quantity) to src/inventory/store.py

## Summary

Add Store.restock, a receiving module that applies delivery notes, and tests.

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
| Unit tests | passed | `sha256:4f314246b0dbb1968cc85b09343c6a39816b16b57fcc6d97278453def12645e4` |
| Integration tests | passed, without network | `sha256:f6744364e9b786f951d5de8de55c2db69cdf4c6d47ae84e4f3abac4736f4652b` |
| Security review | passed | `sha256:1893770df2e7a846cb89bc46989f2f69453b7469f81bbbd7bc89713f0ee7f861` |

## Caught along the way

These checks failed earlier in this run. Each gate passed again on the tree delivered here (Evidence, above).

| gate | tree | what failed | report |
|---|---|---|---|
| Static analysis | `c6b58f2c7d36` | `preferences` | `sha256:8d372c6391a79e9cb550f5347defbdad7f970b0fa47d10412244494235318cc9` |
| Unit tests | `534d536b51ac` | impl_bug | `sha256:2b639d8952c69fdb06ee6e07b8e16c0b823da1b57c266def300fbc08675e19c7` |
| Security review | `4e54e7de7477` | 1 blocking finding(s) | `sha256:23df47e0a075df96e0ba9841a3c8bb6d28fe7ff8aaac7416096eea3113852074` |

## Review

1 finding(s) raised, 1 admitted.

- `c3-1` [major] receive() is not atomic: a bad line raises after the lines before it were applied, so a rejected delivery leaves the store half updated: accepted in negotiation

## Audit

- Run: `run-1` (journal: `msa inspect run-1`)
- Base commit: `f69fc5cb99ad0789f6c543bdce55c7bd99cd00a6`
- Head tree: `6eed0d70e1e13d6e0f34cba1c062718634cb8bd2`
- Approve: `msa approve run-1`; send back: `msa approve run-1 --reject "why"`

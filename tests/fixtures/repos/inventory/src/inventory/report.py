"""Reports over a store."""

from inventory.store import Store


def low_stock(store: Store, threshold: int) -> list[str]:
    """SKUs at or below ``threshold``, in order."""
    return [item.sku for item in store.items() if item.quantity <= threshold]


def to_csv(store: Store) -> str:
    lines = ["sku,quantity", *(f"{i.sku},{i.quantity}" for i in store.items())]
    return "\n".join(lines) + "\n"

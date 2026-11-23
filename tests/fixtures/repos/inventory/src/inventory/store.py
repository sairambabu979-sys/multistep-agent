"""Stock levels, by SKU."""

from inventory.models import Item


class Store:
    def __init__(self) -> None:
        self._items: dict[str, Item] = {}

    def add(self, sku: str, quantity: int) -> None:
        if quantity < 0:
            raise ValueError("quantity cannot be negative")
        self._items[sku] = Item(sku, quantity)

    def quantity(self, sku: str) -> int:
        return self._items[sku].quantity

    def items(self) -> list[Item]:
        return sorted(self._items.values(), key=lambda item: item.sku)

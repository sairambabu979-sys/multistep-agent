from inventory import Store, low_stock


def test_add_and_read() -> None:
    store = Store()
    store.add("apple", 3)
    assert store.quantity("apple") == 3


def test_low_stock_lists_items_at_or_below_the_threshold() -> None:
    store = Store()
    store.add("apple", 1)
    store.add("pear", 9)
    assert low_stock(store, 2) == ["apple"]

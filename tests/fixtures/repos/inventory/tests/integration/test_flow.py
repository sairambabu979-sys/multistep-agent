"""A day's stock, written to disk and read back."""

from pathlib import Path

from inventory import Store, to_csv


def test_a_report_round_trips_through_a_file(tmp_path: Path) -> None:
    store = Store()
    store.add("apple", 3)
    store.add("pear", 0)
    path = tmp_path / "stock.csv"
    path.write_text(to_csv(store), encoding="utf-8")
    assert path.read_text(encoding="utf-8").splitlines() == ["sku,quantity", "apple,3", "pear,0"]

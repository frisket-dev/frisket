import csv as _csv
import sys as _sys


DISTRIBUTION_NAME = "frisket-data"


# CPython's CSV parser otherwise starts with a 128 KiB per-field ceiling. Set
# the process policy once, before any ``frisket.*`` module can construct a CSV
# reader, so all Frisket CSV consumers have the same local-memory-bound
# behavior and individual import requests never mutate shared parser state.
def _configure_csv_field_size_limit() -> int:
    """Lift the parser ceiling to the largest practical cross-platform value."""

    limit = _sys.maxsize
    try:
        _csv.field_size_limit(limit)
    except OverflowError:
        # Windows uses a 32-bit C long even in 64-bit Python.
        limit = (1 << 31) - 1
        _csv.field_size_limit(limit)
    return limit


CSV_FIELD_SIZE_LIMIT = _configure_csv_field_size_limit()


def hello() -> str:
    return "Hello from frisket!"

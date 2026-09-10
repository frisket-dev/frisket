import csv as _csv
import sys as _sys


# CPython's CSV parser otherwise starts with a 128 KiB per-field ceiling. Set
# the process policy once, before any ``frisket.*`` module can construct a CSV
# reader, so all Frisket CSV consumers have the same local-memory-bound
# behavior and individual import requests never mutate shared parser state.
CSV_FIELD_SIZE_LIMIT = _sys.maxsize
_csv.field_size_limit(CSV_FIELD_SIZE_LIMIT)


def hello() -> str:
    return "Hello from frisket!"

"""Unit tests for header iteration edge cases."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from headers_util import iter_headers


class _NameOnlyHeaders:
    """Mimics JS Headers that iterate as header *names* (strings)."""

    def __init__(self, data: dict[str, str]):
        self._data = {k.lower(): (k, v) for k, v in data.items()}

    def keys(self):
        return [orig for orig, _ in self._data.values()]

    def get(self, name: str):
        item = self._data.get(name.lower())
        return None if item is None else item[1]

    def __iter__(self):
        return iter(self.keys())


class _EntriesHeaders:
    def __init__(self, data: dict[str, str]):
        self._pairs = list(data.items())

    def entries(self):
        pairs = self._pairs

        class _Iter:
            def __init__(self):
                self._i = 0

            def next(self):
                class _Nxt:
                    pass

                nxt = _Nxt()
                if self._i >= len(pairs):
                    nxt.done = True
                    nxt.value = None
                    return nxt
                nxt.done = False
                nxt.value = pairs[self._i]
                self._i += 1
                return nxt

        return _Iter()


class IterHeadersTests(unittest.TestCase):
    def test_name_only_iterable_does_not_unpack_strings(self):
        headers = _NameOnlyHeaders(
            {
                "Content-Type": "application/json",
                "X-Custom": "yes",
            }
        )
        got = dict(iter_headers(headers))
        self.assertEqual(got["Content-Type"], "application/json")
        self.assertEqual(got["X-Custom"], "yes")

    def test_entries_api(self):
        headers = _EntriesHeaders({"Accept": "application/json"})
        got = list(iter_headers(headers))
        self.assertEqual(got, [("Accept", "application/json")])

    def test_plain_dict(self):
        got = dict(iter_headers({"A": "1", "B": "2"}))
        self.assertEqual(got, {"A": "1", "B": "2"})


if __name__ == "__main__":
    unittest.main()

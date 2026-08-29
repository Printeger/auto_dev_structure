from __future__ import annotations

import unittest

from greeting import build_greeting


class GreetingTests(unittest.TestCase):
    def test_greets_ada(self) -> None:
        self.assertEqual(build_greeting("Ada"), "Hello, Ada!")

    def test_trims_surrounding_whitespace(self) -> None:
        self.assertEqual(build_greeting("  Grace Hopper\t"), "Hello, Grace Hopper!")

    def test_rejects_a_blank_name(self) -> None:
        with self.assertRaises(ValueError):
            build_greeting(" \t\n")


if __name__ == "__main__":
    unittest.main()

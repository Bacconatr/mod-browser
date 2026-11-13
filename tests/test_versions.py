"""Version ordering and ranges: the pure rules the resolver's verdicts rest on."""

from __future__ import annotations

import unittest

from mod_browser.versions import compare_versions, satisfies


class VersionOrderTest(unittest.TestCase):
    def test_the_main_part_decides_before_the_mod_part(self):
        self.assertEqual(compare_versions("1.21-1.5.7", "1.21.1-1.5.8"), -1)
        self.assertEqual(compare_versions("1.21-1.5.9", "1.21.1-1.5.0"), -1)

    def test_numbers_compare_as_numbers_and_a_release_beats_its_qualifiers(self):
        self.assertEqual(compare_versions("1.9", "1.10"), -1)
        self.assertEqual(compare_versions("1.0-beta", "1.0"), -1)
        self.assertEqual(compare_versions("1.0.0", "1.0.1"), -1)
        self.assertEqual(compare_versions("1.0", "1.0.0"), 0)

    def test_a_v_prefix_and_build_metadata_never_decide(self):
        self.assertEqual(compare_versions("v6.0.10", "6.0.10"), 0)
        self.assertEqual(compare_versions("6.0.10+mc1.21.1", "6.0.10"), 0)


class RangeTest(unittest.TestCase):
    def test_closed_open_and_half_open_intervals(self):
        self.assertTrue(satisfies("1.21.1-1.5.8", "[1.21.1-1.5.8,1.21.2)"))
        self.assertFalse(satisfies("1.21-1.5.7", "[1.21.1-1.5.8,1.21.2)"))
        self.assertFalse(satisfies("1.21.2", "[1.21.1-1.5.8,1.21.2)"))
        self.assertFalse(satisfies("1.0", "(1.0,)"))
        self.assertTrue(satisfies("6.0.0", "[6.0.0,)"))

    def test_an_exact_version_a_bare_version_a_union_and_the_wildcard(self):
        self.assertTrue(satisfies("1.21.1", "[1.21.1]"))
        self.assertFalse(satisfies("1.21.2", "[1.21.1]"))
        self.assertTrue(satisfies("2.0", "1.0"))  # bare means this or newer
        self.assertFalse(satisfies("0.9", "1.0"))
        self.assertTrue(satisfies("3.0", "[1.0,2.0),[3.0,)"))
        self.assertFalse(satisfies("2.5", "[1.0,2.0),[3.0,)"))
        self.assertTrue(satisfies("0.0.1", "*"))
        self.assertTrue(satisfies("0.0.1", ""))

    def test_a_range_nobody_can_read_is_not_a_reason_to_refuse(self):
        self.assertTrue(satisfies("1.0", "${minecraft_version_range}"))


if __name__ == "__main__":
    unittest.main()

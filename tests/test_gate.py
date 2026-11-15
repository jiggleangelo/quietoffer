import unittest

from haggle.gate import check, parse_price


class GateTests(unittest.TestCase):
    def test_allow_categories(self):
        for word in ("monitor", "table", "stroller", "golf", "collectible", "blender", "drill", "jacket"):
            with self.subTest(word=word):
                self.assertTrue(check(word, "100").allowed)

    def test_denials_plural_synonyms_and_mixed_listings(self):
        denied = (
            "cars", "trucks", "rifles", "guns", "pistols", "handgun", "shotgun",
            "revolver", "vapes", "puppies", "tickets", "e-tickets",
        )
        for word in denied:
            with self.subTest(word=word):
                self.assertFalse(check(word, "100").allowed)
        self.assertFalse(check("rifles with a tripod camera", "100", "Camera and rifles").allowed)

    def test_legitimate_phrase_exceptions(self):
        for value in (
            "safe baby car seat", "living room table", "car charger", "toy car",
            "wine rack", "wine glasses", "boat shoes", "beer mug", "cigar box",
            "kids electric scooter", "trailer hitch",
        ):
            with self.subTest(value=value):
                self.assertTrue(check(value, "50", value, allow_general=True).allowed)

    def test_unknown_and_prices(self):
        self.assertFalse(check("mystery object", "100").allowed)
        self.assertEqual(parse_price("$1,234.56"), 123456)
        self.assertEqual(parse_price("1.2k"), 120000)
        self.assertIsNone(parse_price("junk"))
        self.assertTrue(check("monitor", "2000").allowed)
        self.assertTrue(check("monitor", "2000.01").allowed)
        self.assertTrue(check("monitor", "25000").allowed)

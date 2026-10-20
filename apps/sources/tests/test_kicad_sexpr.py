from unittest import mock

from django.test import SimpleTestCase

from apps.sources.kicad import sexpr
from apps.sources.kicad.sexpr import KicadError


class SexprTests(SimpleTestCase):
    def test_parses_nested_and_strings(self):
        tree = sexpr.parse('(a (b "x y" 1.5) c (d "say \\"hi\\" \\\\ ok"))')
        self.assertEqual(tree, ["a", ["b", "x y", "1.5"], "c", ["d", 'say "hi" \\ ok']])
        self.assertIsInstance(tree[1][1], sexpr.Str)
        self.assertNotIsInstance(tree[0], sexpr.Str)

    def test_helpers(self):
        t = sexpr.parse('(symbol (at 10.5 -3 90) (property "Reference" "R1") (property "Value" "10k") (effects (font) hide) (unit 2))')
        self.assertEqual(sexpr.head(t), "symbol")
        self.assertEqual(sexpr.xy(t), (10.5, -3.0, 90.0))
        self.assertEqual(sexpr.value(t, "unit"), "2")
        self.assertEqual(len(sexpr.find_all(t, "property")), 2)
        self.assertTrue(sexpr.has_flag(sexpr.find(t, "effects"), "hide"))
        self.assertTrue(sexpr.has_flag(sexpr.parse("(effects (font) (hide yes))"), "hide"))
        self.assertFalse(sexpr.has_flag(sexpr.parse("(effects (font) (hide no))"), "hide"))
        self.assertEqual(sexpr.xy(sexpr.parse("(x)")), (0.0, 0.0, 0.0))
        self.assertEqual(sexpr.file_version(sexpr.parse("(kicad_sch (version 20231120))")), 20231120)

    def test_num_rejects_bad_values(self):
        for bad in ("nan", "inf", "-inf", "1e9", "abc", None):
            self.assertEqual(sexpr.num(bad, 7.0), 7.0)
        self.assertEqual(sexpr.num("-2.5"), -2.5)

    def test_hostile_inputs(self):
        for text in ("(a (b)", "(a))", "a b", '(a "unterminated)', "", "   "):
            with self.assertRaises(KicadError):
                sexpr.parse(text)
        with self.assertRaises(KicadError):
            sexpr.parse("(" * 10_000 + ")" * 10_000)
        with mock.patch.object(sexpr, "MAX_BYTES", 100), self.assertRaises(KicadError):
            sexpr.parse("(a " + "x" * 200 + ")")
        with mock.patch.object(sexpr, "MAX_NODES", 50), self.assertRaises(KicadError):
            sexpr.parse("(a " + "(b) " * 100 + ")")

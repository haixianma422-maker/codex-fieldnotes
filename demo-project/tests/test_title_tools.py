import unittest

from title_tools import normalize_title


class NormalizeTitleTests(unittest.TestCase):
    def test_chinese_title(self):
        self.assertEqual(normalize_title("本地集成验收"), "本地集成验收")

    def test_preserves_punctuation_and_case(self):
        self.assertEqual(normalize_title("  中文标题：Python 3.13！  "), "中文标题：Python 3.13！")

    def test_repeated_whitespace_and_trim(self):
        for text in ("  本地   集成  验收  ", "\t本地\t\t集成\r\n  验收\n"):
            with self.subTest(text=text):
                self.assertEqual(normalize_title(text), "本地 集成 验收")

    def test_unicode_whitespace(self):
        self.assertEqual(normalize_title("\u3000中文\u3000\u3000标题\u3000"), "中文 标题")

    def test_empty_title(self):
        with self.assertRaises(ValueError):
            normalize_title("")

    def test_whitespace_only_title(self):
        with self.assertRaises(ValueError):
            normalize_title(" \t\r\n\u3000 ")


if __name__ == "__main__":
    unittest.main()

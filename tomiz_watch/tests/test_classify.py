#!/usr/bin/env python3
"""classify() の判定をページの断片で確かめる。ネットワークは使わない。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watch  # noqa: E402

CFG = {
    "in_stock_markers": ["カートに入れる", "在庫あり"],
    "out_of_stock_markers": ["在庫切れ", "再入荷", "入荷待ち"],
    "login_markers": ["ログインID", "パスワードを忘れ", "/customer/account/login"],
}

URL = "https://b2b.tomiz.com/item/00195014"


class TestClassify(unittest.TestCase):
    def test_in_stock(self):
        page = "<html><body><h1>商品</h1><button>カートに入れる</button></body></html>"
        self.assertEqual(watch.classify(CFG, URL, page)[0], watch.IN_STOCK)

    def test_out_of_stock(self):
        page = "<html><body><h1>商品</h1><p>在庫切れ</p></body></html>"
        self.assertEqual(watch.classify(CFG, URL, page)[0], watch.OUT_OF_STOCK)

    def test_login_redirect(self):
        page = '<html><body><form><input name="login[username]"><input type="password"></form>パスワードを忘れた方</body></html>'
        status, _ = watch.classify(CFG, "https://b2b.tomiz.com/customer/account/login", page)
        self.assertEqual(status, watch.LOGIN_REQUIRED)

    def test_ambiguous_is_unknown(self):
        page = "<html><body><button>カートに入れる</button><a>再入荷のお知らせ</a></body></html>"
        self.assertEqual(watch.classify(CFG, URL, page)[0], watch.UNKNOWN)

    def test_script_text_is_ignored_for_out_of_stock(self):
        page = '<html><body><script>var msg="在庫切れ";</script><button>カートに入れる</button></body></html>'
        self.assertEqual(watch.classify(CFG, URL, page)[0], watch.IN_STOCK)

    def test_no_markers_is_unknown(self):
        self.assertEqual(watch.classify(CFG, URL, "<html><body>なにもない</body></html>")[0], watch.UNKNOWN)


if __name__ == "__main__":
    unittest.main()

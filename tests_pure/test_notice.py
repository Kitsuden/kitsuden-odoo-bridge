"""
The notice contract, without Odoo: `python -m unittest discover -s tests_pure` from the repo root.

`KON_ODOO_SIGNATURE` is pinned here and in kon-odoo's tests, where its own `signBody` and
`validSignature` must produce and accept it: if either side changes how it signs, one fails.
"""

import importlib.util
import json
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("notice", HERE.parent / "kitsuden_bridge" / "lib" / "notice.py")
notice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notice)

SECRET = "s3cret-s3cret-s3cret-s3cret"
BODY = b'{"id":"abc:1-2","changes":[{"model":"product.template","ids":[10,11]}]}'
# The same vector is asserted in kitsuden-kons tests/unit/kon-odoo.test.ts against `signBody`.
KON_ODOO_SIGNATURE = "sha256=2697060edc9af3940b30a90c0996dcf5d3a80f2a1c8b042e3429f32b98cdb8e6"


class TestNotice(unittest.TestCase):
    def test_groups_by_model_in_a_stable_order_and_drops_what_kon_odoo_refuses(self):
        body, count = notice.build_notice(
            "abc:1-5",
            [
                ("product.product", 40),
                ("product.template", 11),
                ("product.template", 10),
                ("product.template", 11),
                ("res.users", 2),
                ("sale.order", 7),
                ("product.product", 0),
            ],
        )
        self.assertEqual(count, 4)
        self.assertEqual(
            json.loads(body),
            {
                "id": "abc:1-5",
                "changes": [
                    {"model": "product.template", "ids": [10, 11]},
                    {"model": "product.product", "ids": [40]},
                    {"model": "sale.order", "ids": [7]},
                ],
            },
        )
        # Compact: what is signed is exactly what is sent.
        self.assertNotIn(b" ", body)

    def test_splits_a_model_with_more_ids_than_kon_odoo_takes_in_one_group(self):
        body, count = notice.build_notice("abc:1", [("product.product", i) for i in range(1, 6002)])
        changes = json.loads(body)["changes"]
        self.assertEqual([len(c["ids"]) for c in changes], [5000, 1001])
        self.assertEqual(count, 6001)

    def test_refuses_an_id_kon_odoo_would_refuse(self):
        with self.assertRaises(ValueError):
            notice.build_notice("", [])
        with self.assertRaises(ValueError):
            notice.build_notice("x" * 121, [])

    def test_signs_and_verifies(self):
        signature = notice.sign(BODY, SECRET)
        self.assertRegex(signature, r"^sha256=[0-9a-f]{64}$")
        self.assertTrue(notice.verify(BODY, signature, SECRET))
        self.assertTrue(notice.verify(BODY, signature.upper().replace("SHA256=", "sha256="), SECRET))
        self.assertFalse(notice.verify(BODY + b" ", signature, SECRET))
        self.assertFalse(notice.verify(BODY, signature, "another-secret-another-secret"))
        self.assertFalse(notice.verify(BODY, None, SECRET))

    def test_kon_odoo_computes_the_same_signature(self):
        self.assertEqual(notice.sign(BODY, SECRET), KON_ODOO_SIGNATURE)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""
Run inside an Odoo 19 with this module installed:

    odoo -d test_kitsuden -i kitsuden_bridge --test-enable --test-tags /kitsuden_bridge --stop-after-init

Requests to Kitsuden are replaced: nothing leaves the machine.
"""

from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from ..lib.notice import verify


@tagged("post_install", "-at_install")
class TestKitsudenBridge(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("kitsuden_bridge.webhook_url", "https://kitsuden.test/api/odoo/webhook")
        cls.env["ir.config_parameter"].sudo().set_param("kitsuden_bridge.webhook_secret", "s3cret-s3cret-s3cret-s3cret")
        cls.integration = cls.env["res.users"].create(
            {
                "name": "Kitsuden",
                "login": "kitsuden-integration",
                "group_ids" if "group_ids" in cls.env["res.users"]._fields else "groups_id": [
                    (6, 0, [
                        cls.env.ref("base.group_user").id,
                        cls.env.ref("sales_team.group_sale_salesman_all_leads").id,
                        cls.env.ref("stock.group_stock_user").id,
                        cls.env.ref("account.group_account_invoice").id,
                        cls.env.ref("kitsuden_bridge.group_kitsuden_integration").id,
                    ])
                ],
            }
        )
        cls.partner = cls.env["res.partner"].create({"name": "Ana", "email": "ana@example.com"})
        cls.product = cls.env["product.product"].create(
            {"name": "Camiseta", "list_price": 20.0, "sale_ok": True, "invoice_policy": "order", "taxes_id": [(5, 0, 0)]}
        )
        cls.bridge = cls.env["kitsuden.bridge"].with_user(cls.integration)

    def _order(self, ref="web-1", quantity=2):
        return self.bridge.create_order(ref, self.partner.id, [{"product_id": self.product.id, "quantity": quantity}])

    def test_only_the_integration_user_calls_it(self):
        stranger = self.env["res.users"].create({"name": "X", "login": "x-stranger"})
        with self.assertRaises(AccessError):
            self.env["kitsuden.bridge"].with_user(stranger).create_order("web-x", self.partner.id, [])

    def test_create_order_is_idempotent_on_the_reference(self):
        first = self._order("web-2")
        second = self._order("web-2")
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["already_existed"])
        order = self.env["sale.order"].browse(first["id"])
        self.assertEqual(order.client_order_ref, "web-2")
        self.assertEqual(order.amount_untaxed, 40.0)

    def test_confirm_payment_confirms_invoices_and_pays_once(self):
        made = self._order("web-3")
        answer = self.bridge.confirm_payment(made["id"], "web-3", "40.0000", "tilopay", "pay-1")
        order = self.env["sale.order"].browse(made["id"])
        self.assertEqual(order.state, "sale")
        self.assertTrue(answer["invoice_id"])
        invoice = self.env["account.move"].browse(answer["invoice_id"])
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))

        again = self.bridge.confirm_payment(made["id"], "web-3", "40.0000", "tilopay", "pay-1")
        self.assertTrue(again["already_recorded"])
        self.assertEqual(len(order.invoice_ids), 1)

    def test_a_payment_for_another_amount_changes_nothing(self):
        made = self._order("web-4")
        with self.assertRaises(UserError):
            self.bridge.confirm_payment(made["id"], "web-4", "39.0000", "tilopay", "pay-2")
        self.assertEqual(self.env["sale.order"].browse(made["id"]).state, "draft")

    def test_the_reference_must_match_the_order(self):
        made = self._order("web-5")
        with self.assertRaises(Exception):
            self.bridge.confirm_payment(made["id"], "someone-else", "40.0000", "tilopay", "pay-3")

    def test_store_credit_needs_a_wallet_that_holds_it(self):
        made = self._order("web-6")
        with self.assertRaises(UserError):
            self.bridge.confirm_payment(made["id"], "web-6", "30.0000", "tilopay", "pay-4", wallet_amount="10.0000")

    def test_a_change_is_queued_in_the_same_transaction_and_sent_signed(self):
        Outbox = self.env["kitsuden.bridge.outbox"].sudo()
        Outbox.search([]).unlink()
        self.product.product_tmpl_id.write({"list_price": 25.0})
        self.product.product_tmpl_id.write({"list_price": 26.0})
        rows = Outbox.search([("model", "=", "product.template"), ("res_id", "=", self.product.product_tmpl_id.id)])
        self.assertEqual(len(rows), 1, "the same record pending twice is one row")

        sent = []

        class Answer:
            status_code = 200
            text = "{}"

        def post(url, data=None, headers=None, **kwargs):
            sent.append((url, data, headers))
            return Answer()

        # The action commits after each batch; a test's cursor may not.
        with patch("odoo.addons.kitsuden_bridge.models.outbox.requests.post", side_effect=post), patch.object(
            type(self.env["ir.cron"]), "_commit_progress", lambda cron, *args, **kwargs: None
        ):
            Outbox._cron_send()

        self.assertEqual(len(sent), 1)
        url, body, headers = sent[0]
        self.assertEqual(url, "https://kitsuden.test/api/odoo/webhook")
        self.assertTrue(verify(body, headers["X-Odoo-Signature"], "s3cret-s3cret-s3cret-s3cret"))
        self.assertIn(b'"product.template"', body)
        self.assertFalse(Outbox.search([("model", "=", "product.template"), ("res_id", "=", self.product.product_tmpl_id.id)]))

    def test_a_refused_notice_stays_for_the_next_run(self):
        Outbox = self.env["kitsuden.bridge.outbox"].sudo()
        self.product.product_tmpl_id.write({"list_price": 27.0})

        class Refused:
            status_code = 401
            text = "Invalid signature"

        with patch("odoo.addons.kitsuden_bridge.models.outbox.requests.post", return_value=Refused()):
            Outbox._cron_send()
        self.assertTrue(Outbox.search([("model", "=", "product.template")]))

    def test_nothing_is_queued_when_the_bridge_is_not_configured(self):
        Outbox = self.env["kitsuden.bridge.outbox"].sudo()
        Outbox.search([]).unlink()
        self.env["ir.config_parameter"].sudo().set_param("kitsuden_bridge.webhook_url", False)
        self.product.product_tmpl_id.write({"list_price": 28.0})
        self.assertFalse(Outbox.search([]))


@tagged("post_install", "-at_install")
class TestKitsudenBridgeWallet(TransactionCase):
    """Store credit, against Odoo's own loyalty module. Skipped when sale_loyalty is not installed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "loyalty.card" not in cls.env or "reward_id" not in cls.env["sale.order.line"]._fields:
            return
        users = cls.env["res.users"]
        cls.integration = users.create(
            {
                "name": "Kitsuden wallet",
                "login": "kitsuden-wallet",
                "group_ids" if "group_ids" in users._fields else "groups_id": [
                    (6, 0, [
                        cls.env.ref("base.group_user").id,
                        cls.env.ref("sales_team.group_sale_salesman_all_leads").id,
                        cls.env.ref("stock.group_stock_user").id,
                        cls.env.ref("account.group_account_invoice").id,
                        cls.env.ref("kitsuden_bridge.group_kitsuden_integration").id,
                    ])
                ],
            }
        )
        cls.partner = cls.env["res.partner"].create({"name": "Bea", "email": "bea@example.com"})
        cls.product = cls.env["product.product"].create(
            {"name": "Sobre", "list_price": 30.0, "sale_ok": True, "invoice_policy": "order", "taxes_id": [(5, 0, 0)]}
        )
        # As Odoo's own eWallet template makes it: one point per unit of money, spent as a discount.
        cls.program = cls.env["loyalty.program"].create(
            {
                "name": "eWallet",
                "program_type": "ewallet",
                "applies_on": "future",
                "trigger": "auto",
                "reward_ids": [(0, 0, {
                    "reward_type": "discount",
                    "discount_mode": "per_point",
                    "discount": 1,
                    "discount_applicability": "order",
                    "required_points": 1,
                    "description": "eWallet",
                })],
            }
        )
        cls.card = cls.env["loyalty.card"].create(
            {"program_id": cls.program.id, "partner_id": cls.partner.id, "points": 50}
        )
        cls.bridge = cls.env["kitsuden.bridge"].with_user(cls.integration)

    def setUp(self):
        super().setUp()
        if not hasattr(self, "card"):
            self.skipTest("sale_loyalty is not installed")

    def test_store_credit_and_card_pay_one_order_once(self):
        made = self.bridge.create_order("wallet-1", self.partner.id, [{"product_id": self.product.id, "quantity": 2}])
        order = self.env["sale.order"].browse(made["id"])
        self.assertEqual(order.amount_total, 60.0)

        answer = self.bridge.confirm_payment(made["id"], "wallet-1", "40.0000", "tilopay", "w-1", wallet_amount="20.0000")
        order.invalidate_recordset()
        self.card.invalidate_recordset()
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.amount_total, 40.0, "the store credit is a line on the order")
        self.assertEqual(self.card.points, 30.0, "the points are taken once")
        invoice = self.env["account.move"].browse(answer["invoice_id"])
        self.assertEqual(invoice.amount_total, 40.0)
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))

        again = self.bridge.confirm_payment(made["id"], "wallet-1", "40.0000", "tilopay", "w-1", wallet_amount="20.0000")
        self.assertTrue(again["already_recorded"])
        self.card.invalidate_recordset()
        self.assertEqual(self.card.points, 30.0)

    def test_more_store_credit_than_the_card_holds_changes_nothing(self):
        made = self.bridge.create_order("wallet-2", self.partner.id, [{"product_id": self.product.id, "quantity": 3}])
        with self.assertRaises(UserError):
            self.bridge.confirm_payment(made["id"], "wallet-2", "10.0000", "tilopay", "w-2", wallet_amount="80.0000")
        self.card.invalidate_recordset()
        self.assertEqual(self.card.points, 50.0)

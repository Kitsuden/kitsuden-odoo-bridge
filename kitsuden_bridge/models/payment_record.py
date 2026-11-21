# -*- coding: utf-8 -*-
"""
Each payment Kitsuden reported, recorded once.

kon-payments and kon-odoo retry: the same payment can reach ``confirm_payment`` twice. The pair
(provider, reference) is unique here, so the second call answers what the first one did instead
of confirming, invoicing and charging the eWallet again.
"""

from odoo import fields, models


class KitsudenBridgePayment(models.Model):
    _name = "kitsuden.bridge.payment"
    _description = "Payment reported by Kitsuden"
    _order = "id desc"

    provider = fields.Char(required=True, readonly=True)
    reference = fields.Char(required=True, readonly=True)
    order_id = fields.Many2one("sale.order", required=True, readonly=True, ondelete="restrict")
    amount = fields.Monetary(readonly=True, currency_field="currency_id")
    wallet_amount = fields.Monetary(readonly=True, currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", readonly=True)
    invoice_id = fields.Many2one("account.move", readonly=True)
    payment_id = fields.Many2one("account.payment", readonly=True)
    loyalty_card_id = fields.Integer(readonly=True)

    def init(self):
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS kitsuden_bridge_payment_reference_idx "
            "ON kitsuden_bridge_payment (provider, reference)"
        )

    def as_answer(self):
        self.ensure_one()
        return {
            "order_id": self.order_id.id,
            "order_name": self.order_id.name,
            "invoice_id": self.invoice_id.id or False,
            "payment_id": self.payment_id.id or False,
            "wallet_amount": self.wallet_amount,
            "already_recorded": False,
        }

# -*- coding: utf-8 -*-
"""
The records Kitsuden mirrors, and the moment each one changes.

Each override writes to the outbox and nothing else: no HTTP, no exception of its own. What
counts as a change is deliberately broad (any write to a product): a notice only makes Kitsuden
read the record again, so one too many costs a read, and one too few leaves the shop wrong until
the nightly walk.
"""

from odoo import api, models

OUTBOX = "kitsuden.bridge.outbox"

#: Fields of a quant whose change moves what can be sold.
STOCK_FIELDS = {"quantity", "reserved_quantity", "location_id", "product_id"}
#: Fields of an order that change what the shop shows about it.
ORDER_FIELDS = {"state", "amount_total", "order_line", "partner_id"}


class ProductTemplate(models.Model):
    _inherit = "product.template"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.env[OUTBOX].enqueue("product.template", records.ids)
        return records

    def write(self, vals):
        result = super().write(vals)
        self.env[OUTBOX].enqueue("product.template", self.ids)
        return result

    def unlink(self):
        ids = self.ids
        result = super().unlink()
        self.env[OUTBOX].enqueue("product.template", ids)
        return result


class ProductProduct(models.Model):
    _inherit = "product.product"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.env[OUTBOX].enqueue("product.product", records.ids)
        return records

    def write(self, vals):
        result = super().write(vals)
        self.env[OUTBOX].enqueue("product.product", self.ids)
        return result

    def unlink(self):
        # A deleted variant is read back as its template: Kitsuden hides what Odoo no longer has.
        templates = self.product_tmpl_id.ids
        result = super().unlink()
        self.env[OUTBOX].enqueue("product.template", templates)
        return result


class ProductCategory(models.Model):
    _inherit = "product.category"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.env[OUTBOX].enqueue("product.category", records.ids)
        return records

    def write(self, vals):
        result = super().write(vals)
        self.env[OUTBOX].enqueue("product.category", self.ids)
        return result

    def unlink(self):
        ids = self.ids
        result = super().unlink()
        self.env[OUTBOX].enqueue("product.category", ids)
        return result


class StockQuant(models.Model):
    """Stock moves through quants: a sale at the counter, a receipt, an adjustment, a reservation."""

    _inherit = "stock.quant"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.env[OUTBOX].enqueue("product.product", records.product_id.ids)
        return records

    def write(self, vals):
        before = self.product_id.ids if "product_id" in vals else []
        result = super().write(vals)
        if STOCK_FIELDS.intersection(vals):
            self.env[OUTBOX].enqueue("product.product", before + self.product_id.ids)
        return result

    def unlink(self):
        products = self.product_id.ids
        result = super().unlink()
        self.env[OUTBOX].enqueue("product.product", products)
        return result


class SaleOrder(models.Model):
    _inherit = "sale.order"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.env[OUTBOX].enqueue("sale.order", records.ids)
        return records

    def write(self, vals):
        result = super().write(vals)
        if ORDER_FIELDS.intersection(vals):
            self.env[OUTBOX].enqueue("sale.order", self.ids)
        return result


class AccountPayment(models.Model):
    _inherit = "account.payment"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.env[OUTBOX].enqueue("account.payment", records.filtered(lambda p: p.payment_type == "inbound").ids)
        return records

    def write(self, vals):
        result = super().write(vals)
        if "state" in vals or "is_reconciled" in vals:
            self.env[OUTBOX].enqueue("account.payment", self.filtered(lambda p: p.payment_type == "inbound").ids)
        return result

# -*- coding: utf-8 -*-
"""
What kon-odoo calls, through Odoo's JSON-2 API:

    POST /json/2/kitsuden.bridge/create_order      { client_ref, partner_id, lines, note, confirm }
    POST /json/2/kitsuden.bridge/confirm_payment   { order_id, client_ref, amount, provider,
                                                     reference, wallet_amount }

Each JSON-2 call is one database transaction. That is the point of these two methods: what must
happen together (confirm the order, take the store credit, invoice it, record the payment) is one
call, so it all happens or none of it does. Called with separate generic calls, a failure halfway
would leave an order confirmed and unpaid, or store credit taken from a customer for nothing.

Who may call: a user in the group "Kitsuden integration" (or an administrator). The work itself
runs with that user's own rights, so Odoo's record rules and its audit trail apply: the user also
needs to sell, invoice and record payments. Only the bridge's own bookkeeping runs as superuser.

`fields_get` on this model is how kon-odoo learns that the bridge is installed.
"""

import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Command
from odoo.tools import float_compare, float_is_zero

_logger = logging.getLogger(__name__)

GROUP = "kitsuden_bridge.group_kitsuden_integration"
PARAM_JOURNAL = "kitsuden_bridge.payment_journal_id"


class KitsudenBridge(models.AbstractModel):
    _name = "kitsuden.bridge"
    _description = "Kitsuden bridge"

    # ── Who ───────────────────────────────────────────────────────────────────

    def _check_caller(self):
        if not (self.env.is_admin() or self.env.user.has_group(GROUP)):
            raise AccessError("Only the Kitsuden integration user may call the Kitsuden bridge.")

    # ── Orders ────────────────────────────────────────────────────────────────

    @api.model
    def create_order(self, client_ref, partner_id, lines, note="", confirm=False):
        """
        Makes a web order, or answers the one already made with this reference: kon-odoo retries,
        and a retry must not make a second order.
        """
        self._check_caller()
        client_ref = (client_ref or "").strip()
        if not client_ref or len(client_ref) > 120:
            raise ValidationError("client_ref is required, at most 120 characters.")
        if not lines:
            raise ValidationError("An order needs at least one line.")

        Order = self.env["sale.order"]
        existing = Order.search([("client_order_ref", "=", client_ref)], limit=1)
        if existing:
            return {"id": existing.id, "name": existing.name, "already_existed": True}

        partner = self.env["res.partner"].browse(int(partner_id)).exists()
        if not partner:
            raise ValidationError("Unknown customer %s." % partner_id)

        commands = []
        for index, line in enumerate(lines, start=1):
            try:
                product_id = int(line["product_id"])
                quantity = float(line["quantity"])
            except (KeyError, TypeError, ValueError):
                raise ValidationError("Line %s needs product_id and quantity." % index) from None
            if quantity <= 0:
                raise ValidationError("Line %s: the quantity must be above zero." % index)
            product = self.env["product.product"].browse(product_id).exists()
            if not product or not product.sale_ok:
                raise ValidationError("Line %s: product %s cannot be sold." % (index, product_id))
            commands.append(Command.create({"product_id": product.id, "product_uom_qty": quantity}))

        values = {"partner_id": partner.id, "client_order_ref": client_ref, "order_line": commands}
        if note:
            values["note"] = note
        order = Order.create(values)
        if confirm:
            order.action_confirm()
        return {"id": order.id, "name": order.name, "already_existed": False}

    # ── Payments ──────────────────────────────────────────────────────────────

    @api.model
    def confirm_payment(self, order_id, client_ref, amount, provider, reference, wallet_amount=0):
        """
        A web order was paid. In one transaction: takes the store credit from the customer's
        eWallet, checks that what Odoo says the order costs is what was paid, confirms the order,
        invoices it when it can be invoiced, and records the payment. The same payment twice
        answers the first one's result.
        """
        self._check_caller()
        provider = (provider or "").strip()[:32]
        reference = (reference or "").strip()[:191]
        if not provider or not reference:
            raise ValidationError("provider and reference are required.")

        Record = self.env["kitsuden.bridge.payment"].sudo()
        done = Record.search([("provider", "=", provider), ("reference", "=", reference)], limit=1)
        if done:
            answer = done.as_answer()
            answer["already_recorded"] = True
            return answer

        order = self.env["sale.order"].browse(int(order_id)).exists()
        if not order:
            raise ValidationError("Unknown order %s." % order_id)
        if client_ref and order.client_order_ref != client_ref:
            raise ValidationError("Order %s is not the order %s." % (order.name, client_ref))
        if order.state == "cancel":
            raise UserError("Order %s is cancelled in Odoo: it cannot be paid." % order.name)

        currency = order.currency_id
        paid = float(amount or 0)
        wallet = float(wallet_amount or 0)
        if paid < 0 or wallet < 0:
            raise ValidationError("Amounts cannot be negative.")

        # Confirmed FIRST. Odoo's loyalty module recomputes eWallet lines when an order is
        # confirmed, and always to the most the card can pay: a line for part of the balance
        # added before would come back as the whole balance. Added to a confirmed order, the
        # line keeps its amount and Odoo itself takes exactly those points off the card.
        # Anything below that fails undoes the confirmation too: it is all one transaction.
        if order.state in ("draft", "sent"):
            order.action_confirm()

        card = None
        if not float_is_zero(wallet, precision_rounding=currency.rounding):
            card = self._wallet_card(order, wallet)
            points_before = card.points
            self._add_wallet_line(order, card, wallet)
            self._consume_wallet(order, card, wallet, points_before)

        # What Odoo says the order costs, after the store credit, must be what the customer paid.
        if float_compare(order.amount_total, paid, precision_rounding=currency.rounding) != 0:
            raise UserError(
                "Order %s costs %s %s in Odoo, and %s was paid."
                % (order.name, order.amount_total, currency.name, paid)
            )

        invoice, payment = self._record_payment(order, paid, provider, reference)
        record = Record.create(
            {
                "provider": provider,
                "reference": reference,
                "order_id": order.id,
                "amount": paid,
                "wallet_amount": wallet,
                "currency_id": currency.id,
                "invoice_id": invoice.id if invoice else False,
                "payment_id": payment.id if payment else False,
                "loyalty_card_id": card.id if card else 0,
            }
        )
        order.message_post(
            body="Paid on the web: %s %s with %s (%s)%s."
            % (paid, currency.name, provider, reference, (", plus %s of store credit" % wallet) if wallet else "")
        )
        return record.as_answer()

    # ── Store credit ──────────────────────────────────────────────────────────

    def _wallet_card(self, order, amount):
        if "loyalty.card" not in self.env:
            raise UserError("Store credit needs the Loyalty module (sale_loyalty) in Odoo.")
        partner = order.partner_id.commercial_partner_id
        cards = self.env["loyalty.card"].search(
            [
                ("partner_id", "child_of", partner.id),
                ("program_id.program_type", "=", "ewallet"),
                ("points", ">=", amount),
            ],
            order="expiration_date asc, id asc",
        )
        today = fields.Date.context_today(self)
        for card in cards:
            if not card.expiration_date or card.expiration_date >= today:
                return card
        raise UserError("%s has no eWallet with %s of store credit." % (partner.display_name, amount))

    def _add_wallet_line(self, order, card, amount):
        """
        Puts the store credit on the (confirmed) order as the eWallet program's own discount line,
        so the order, its invoice and its total say how it was paid.
        """
        reward = card.program_id.reward_ids[:1]
        product = reward.discount_line_product_id if reward else False
        if not product:
            raise UserError("The eWallet program %s has no discount product to put on the order." % card.program_id.display_name)

        line_values = {
            "order_id": order.id,
            "product_id": product.id,
            "name": "eWallet: %s" % card.program_id.name,
            "product_uom_qty": 1,
            "price_unit": -amount,
        }
        Line = self.env["sale.order.line"]
        # Store credit is money already received: no tax on it. (The field is `tax_id` up to
        # Odoo 18 and `tax_ids` after.)
        line_values["tax_ids" if "tax_ids" in Line._fields else "tax_id"] = [Command.clear()]
        # The fields sale_loyalty uses to know a line is a reward paid with that card.
        if "reward_id" in Line._fields:
            line_values["reward_id"] = reward.id
        if "coupon_id" in Line._fields:
            line_values["coupon_id"] = card.id
        if "points_cost" in Line._fields:
            line_values["points_cost"] = amount
        Line.create(line_values)

    def _consume_wallet(self, order, card, amount, points_before):
        """
        Takes the points off the card, unless Odoo's loyalty module already did when the order
        was confirmed: never twice.
        """
        card.invalidate_recordset(["points"])
        if float_compare(card.points, points_before, precision_digits=4) != 0:
            return
        if float_compare(card.points, amount, precision_digits=4) < 0:
            raise UserError("The eWallet of %s no longer holds %s." % (order.partner_id.display_name, amount))
        card.sudo().points = card.points - amount
        if "loyalty.history" in self.env:
            History = self.env["loyalty.history"].sudo()
            values = {"card_id": card.id, "description": "Web order %s" % order.name, "used": amount}
            if "order_model" in History._fields:
                values.update({"order_model": "sale.order", "order_id": order.id})
            History.create(values)

    # ── Recording the money ───────────────────────────────────────────────────

    def _payment_journal(self, order):
        journal_id = self.env["ir.config_parameter"].sudo().get_param(PARAM_JOURNAL)
        Journal = self.env["account.journal"]
        if journal_id:
            journal = Journal.browse(int(journal_id)).exists()
            if journal:
                return journal
        journal = Journal.search(
            [("type", "=", "bank"), ("company_id", "=", order.company_id.id)], order="sequence, id", limit=1
        )
        if not journal:
            raise UserError("Choose the journal web payments go to (Settings > Kitsuden).")
        return journal

    def _record_payment(self, order, amount, provider, reference):
        """
        Invoices the order when it can be invoiced now and registers the payment on the invoice.
        When it cannot (invoicing on delivered quantities), records a customer payment with the
        order's name in its memo, for the invoice to be reconciled with it later.
        """
        if float_is_zero(amount, precision_rounding=order.currency_id.rounding):
            return None, None
        journal = self._payment_journal(order)
        memo = "%s %s %s" % (order.name, provider, reference)

        invoice = self.env["account.move"]
        if order.invoice_status == "to invoice":
            invoice = order._create_invoices()
            invoice.action_post()

        if invoice:
            wizard = (
                self.env["account.payment.register"]
                .with_context(active_model="account.move", active_ids=invoice.ids)
                .create(
                    {
                        "journal_id": journal.id,
                        "amount": amount,
                        "payment_date": fields.Date.context_today(self),
                        "communication": memo,
                    }
                )
            )
            payments = wizard._create_payments()
            return invoice[:1], payments[:1]

        Payment = self.env["account.payment"]
        values = {
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": order.partner_id.commercial_partner_id.id,
            "amount": amount,
            "currency_id": order.currency_id.id,
            "journal_id": journal.id,
            "date": fields.Date.context_today(self),
            # Odoo 18 renamed `ref` to `memo` on payments.
            ("memo" if "memo" in Payment._fields else "ref"): memo,
        }
        payment = Payment.create(values)
        payment.action_post()
        return None, payment

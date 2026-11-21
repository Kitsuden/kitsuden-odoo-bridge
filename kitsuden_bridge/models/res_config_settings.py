# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    kitsuden_webhook_url = fields.Char(
        string="Kitsuden notice address",
        config_parameter="kitsuden_bridge.webhook_url",
        help="Kitsuden's POST /api/odoo/webhook. Empty: nothing is queued and nothing is sent.",
    )
    kitsuden_webhook_secret = fields.Char(
        string="Kitsuden notice secret",
        config_parameter="kitsuden_bridge.webhook_secret",
        help="The same secret stored in kon-odoo's settings (webhookSecret). Signs every notice.",
        groups="base.group_system",
    )
    kitsuden_payment_journal_id = fields.Many2one(
        "account.journal",
        string="Journal for web payments",
        config_parameter="kitsuden_bridge.payment_journal_id",
        domain="[('type', 'in', ('bank', 'cash'))]",
        help="Where payments taken on the web are recorded. Empty: the company's first bank journal.",
    )

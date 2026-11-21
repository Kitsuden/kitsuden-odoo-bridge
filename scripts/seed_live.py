# -*- coding: utf-8 -*-
"""
A shop to point kon-odoo at, for its live test. Run inside Odoo's shell:

    docker compose exec -T odoo odoo shell -d live_kitsuden --db_host db --db_user odoo \
        --db_password odoo --no-http < scripts/seed_live.py

Creates the integration user, an API key for it (written to /var/lib/odoo/kitsuden-live-key,
never printed), a shirt in two colours with stock, a service, a customer with an eWallet, and
points the bridge at a webhook URL so the outbox fills. Safe to run twice.
"""

import datetime

from odoo import Command

env = env  # noqa: F821  (the shell provides it)

ref = env.ref
users = env["res.users"]
groups_field = "group_ids" if "group_ids" in users._fields else "groups_id"

user = users.search([("login", "=", "kitsuden-live")], limit=1)
if not user:
    user = users.create({"name": "Kitsuden live", "login": "kitsuden-live", "email": "kitsuden-live@example.com"})
user.write(
    {
        groups_field: [
            Command.link(ref("base.group_user").id),
            Command.link(ref("sales_team.group_sale_salesman_all_leads").id),
            Command.link(ref("stock.group_stock_user").id),
            Command.link(ref("account.group_account_invoice").id),
            Command.link(ref("kitsuden_bridge.group_kitsuden_integration").id),
        ]
    }
)

# Variants and storage locations must be switched on for the shop to have them.
config = env["res.config.settings"].create({"group_product_variant": True, "group_stock_multi_locations": False})
config.execute()

attribute = env["product.attribute"].search([("name", "=", "Color")], limit=1) or env["product.attribute"].create(
    {"name": "Color", "create_variant": "always", "value_ids": [Command.create({"name": "Red"}), Command.create({"name": "Blue"})]}
)
category = env["product.category"].search([("name", "=", "Clothing")], limit=1) or env["product.category"].create({"name": "Clothing"})

shirt = env["product.template"].search([("default_code", "=", "LIVE-TEE")], limit=1)
if not shirt:
    shirt = env["product.template"].create(
        {
            "name": "Camiseta Live",
            "default_code": "LIVE-TEE",
            "list_price": 20.0,
            "categ_id": category.id,
            "is_storable": True,
            "sale_ok": True,
            "invoice_policy": "order",
            "taxes_id": [Command.clear()],
            "attribute_line_ids": [Command.create({"attribute_id": attribute.id, "value_ids": [Command.set(attribute.value_ids.ids)]})],
        }
    )
    if "is_published" in shirt._fields:
        shirt.is_published = True
    for variant in shirt.product_variant_ids:
        colour = variant.product_template_attribute_value_ids[:1].name
        variant.default_code = "LIVE-TEE-%s" % colour.upper()
    warehouse = env["stock.warehouse"].search([], limit=1)
    for variant, quantity in zip(shirt.product_variant_ids, (5, 1)):
        env["stock.quant"]._update_available_quantity(variant, warehouse.lot_stock_id, quantity)

service = env["product.template"].search([("default_code", "=", "LIVE-WRAP")], limit=1)
if not service:
    service = env["product.template"].create(
        {"name": "Envoltorio Live", "default_code": "LIVE-WRAP", "list_price": 3.0, "type": "service", "sale_ok": True, "taxes_id": [Command.clear()]}
    )
    if "is_published" in service._fields:
        service.is_published = True

partner = env["res.partner"].search([("email", "=", "live@example.com")], limit=1) or env["res.partner"].create(
    {"name": "Cliente Live", "email": "live@example.com"}
)
if "loyalty.program" in env:
    program = env["loyalty.program"].search([("program_type", "=", "ewallet")], limit=1)
    if not program:
        program = env["loyalty.program"].create(
            {
                "name": "eWallet",
                "program_type": "ewallet",
                "applies_on": "future",
                "trigger": "auto",
                "reward_ids": [Command.create({"reward_type": "discount", "discount_mode": "per_point", "discount": 1, "discount_applicability": "order", "required_points": 1, "description": "eWallet"})],
            }
        )
    if not env["loyalty.card"].search([("program_id", "=", program.id), ("partner_id", "=", partner.id)]):
        env["loyalty.card"].create({"program_id": program.id, "partner_id": partner.id, "points": 50})

params = env["ir.config_parameter"].sudo()
params.set_param("kitsuden_bridge.webhook_url", "http://kitsuden.invalid/api/odoo/webhook")
params.set_param("kitsuden_bridge.webhook_secret", "live-secret-live-secret-live-secret")

# A key for the integration user, made as that user. Written to a file, never printed.
expires = fields_date = datetime.date.today() + datetime.timedelta(days=30)
key = env["res.users.apikeys"].with_user(user)._generate(None, "kitsuden live test", datetime.datetime.combine(expires, datetime.time()))
with open("/var/lib/odoo/kitsuden-live-key", "w") as handle:
    handle.write(key)

env.cr.commit()
print("seeded: templates", shirt.id, service.id, "variants", shirt.product_variant_ids.ids, "partner", partner.id)

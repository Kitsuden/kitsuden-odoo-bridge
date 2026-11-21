# -*- coding: utf-8 -*-
{
    "name": "Kitsuden Bridge",
    "summary": "Tells Kitsuden what changed, and confirms paid web orders in one transaction",
    "description": """
Kitsuden Bridge
===============

The Odoo side of kon-odoo (Kitsuden). Two jobs:

* **Change notices.** When a product, a variant, a category, the stock of a variant, a sales
  order or a customer payment changes, a row is written to an outbox in the same transaction.
  A scheduled action sends what is pending to Kitsuden as one signed notice. A change that
  Odoo rolls back is never announced; one Kitsuden did not receive is sent again.
* **One call, one transaction.** ``kitsuden.bridge.create_order`` makes a web order, and
  ``kitsuden.bridge.confirm_payment`` confirms a paid order, takes the store credit from the
  customer's eWallet and records the payment, all or nothing.
""",
    "version": "19.0.1.0.0",
    "category": "Sales",
    "license": "LGPL-3",
    "author": "Kitsuden",
    "website": "https://github.com/Kitsuden",
    "depends": ["sale", "stock", "account"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "application": False,
}

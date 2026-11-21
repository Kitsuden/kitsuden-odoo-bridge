# -*- coding: utf-8 -*-
"""
The change notice kon-odoo accepts, built and signed.

No Odoo import here on purpose: this is the contract with Kitsuden, and it is tested on its own
(``tests_pure/``), byte for byte against what kon-odoo verifies.

    POST <webhook url>
    Content-Type: application/json
    X-Odoo-Signature: sha256=<hex HMAC-SHA256 of the exact body, with the shared secret>

    {"id": "<unique per notice>",
     "changes": [{"model": "product.template", "ids": [12, 13]}, ...]}

kon-odoo refuses a notice with a model it does not know, more than 50 groups, more than 5000 ids
in a group, or more than 256 KiB.
"""

import hashlib
import hmac
import json

#: Models kon-odoo accepts in a notice. A stock change is announced as its variant.
MODELS = (
    "product.template",
    "product.product",
    "product.category",
    "sale.order",
    "account.payment",
)

MAX_GROUPS = 50
MAX_IDS_PER_GROUP = 5000
MAX_BYTES = 256 * 1024

SIGNATURE_HEADER = "X-Odoo-Signature"


def build_notice(notice_id, rows):
    """
    The notice for these outbox rows: ``rows`` is an iterable of ``(model, res_id)``.

    Rows of a model kon-odoo does not accept are left out. Returns the body as bytes (what is
    signed is exactly what is sent) and the number of ids in it.
    """
    if not notice_id or len(notice_id) > 120:
        raise ValueError("A notice id is 1 to 120 characters")
    groups = {}
    for model, res_id in rows:
        if model not in MODELS:
            continue
        res_id = int(res_id)
        if res_id <= 0:
            continue
        groups.setdefault(model, set()).add(res_id)

    changes = []
    for model in MODELS:
        ids = sorted(groups.get(model, ()))
        for start in range(0, len(ids), MAX_IDS_PER_GROUP):
            changes.append({"model": model, "ids": ids[start:start + MAX_IDS_PER_GROUP]})
    if len(changes) > MAX_GROUPS:
        raise ValueError("Too many groups for one notice: send fewer rows at a time")

    body = json.dumps({"id": notice_id, "changes": changes}, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_BYTES:
        raise ValueError("The notice is too large: send fewer rows at a time")
    return body, sum(len(change["ids"]) for change in changes)


def sign(body, secret):
    """``sha256=<hex>`` of the body, as kon-odoo's ``validSignature`` checks it."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def verify(body, header, secret):
    """The other side of ``sign``, for tests and for anyone receiving a notice."""
    if not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(sign(body, secret), header.strip().lower())

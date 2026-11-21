# -*- coding: utf-8 -*-
"""
The outbox: what changed, written in the same transaction as the change.

A change is not sent to Kitsuden from inside the transaction that makes it. If it were, a slow or
absent Kitsuden would slow every sale at the point of sale, and a transaction that rolls back
would already have announced a change that never happened. Instead one row per changed record
goes in this table, and commits or rolls back with the change. A scheduled action sends what is
pending.

One pending row per record: the same product written ten times before the next send is one row.
"""

import logging
import uuid

import requests

from odoo import api, fields, models

from ..lib.notice import SIGNATURE_HEADER, build_notice, sign

_logger = logging.getLogger(__name__)

PARAM_URL = "kitsuden_bridge.webhook_url"
PARAM_SECRET = "kitsuden_bridge.webhook_secret"
PARAM_DB_KEY = "kitsuden_bridge.notice_prefix"

#: Rows sent in one notice. Well under kon-odoo's limits (50 groups, 5000 ids, 256 KiB).
BATCH = 500
#: Seconds to wait for Kitsuden before trying again at the next run.
TIMEOUT = 10


class KitsudenBridgeOutbox(models.Model):
    _name = "kitsuden.bridge.outbox"
    _description = "Kitsuden change to send"
    _order = "id"
    _log_access = False

    model = fields.Char(required=True, index=False)
    res_id = fields.Integer(required=True)
    queued_at = fields.Datetime(default=fields.Datetime.now, required=True)

    def init(self):
        # The same record pending twice is one row: `enqueue` relies on this index.
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS kitsuden_bridge_outbox_record_idx "
            "ON kitsuden_bridge_outbox (model, res_id)"
        )

    # ── Writing ───────────────────────────────────────────────────────────────

    @api.model
    def enqueue(self, model, ids):
        """
        Records that these records of ``model`` changed. Cheap and silent: one INSERT, nothing
        when the bridge is not configured, and never an error that would undo the change itself.
        """
        ids = [int(res_id) for res_id in ids if res_id]
        if not ids:
            return
        params = self.env["ir.config_parameter"].sudo()
        if not params.get_param(PARAM_URL):
            return
        self.env.cr.execute(
            """
            INSERT INTO kitsuden_bridge_outbox (model, res_id, queued_at)
            SELECT %s, unnest(%s::int[]), now() AT TIME ZONE 'UTC'
            ON CONFLICT (model, res_id) DO NOTHING
            """,
            (model, ids),
        )
        # Send soon, once per transaction: the trigger is part of this transaction, so a change
        # that rolls back schedules nothing.
        data = self.env.cr.precommit.data
        if not data.get("kitsuden_bridge.triggered"):
            data["kitsuden_bridge.triggered"] = True
            cron = self.env.ref("kitsuden_bridge.ir_cron_send_outbox", raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()

    # ── Sending ───────────────────────────────────────────────────────────────

    def _notice_prefix(self):
        """A prefix that tells this database's notices apart from another's, made once."""
        params = self.env["ir.config_parameter"].sudo()
        prefix = params.get_param(PARAM_DB_KEY)
        if not prefix:
            prefix = uuid.uuid4().hex[:12]
            params.set_param(PARAM_DB_KEY, prefix)
        return prefix

    @api.model
    def _cron_send(self, max_batches=20):
        """
        Sends what is pending, a batch per notice. A batch Kitsuden did not take stays for the next
        run, under the same notice id, so a notice that arrived but whose answer was lost is
        recognised and dropped by Kitsuden.
        """
        params = self.env["ir.config_parameter"].sudo()
        url = params.get_param(PARAM_URL)
        secret = params.get_param(PARAM_SECRET)
        if not url or not secret:
            return
        prefix = self._notice_prefix()

        for _ in range(max_batches):
            # Two workers running the action at once take different rows.
            self.env.cr.execute(
                "SELECT id, model, res_id FROM kitsuden_bridge_outbox ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
                (BATCH,),
            )
            rows = self.env.cr.fetchall()
            if not rows:
                return
            first, last = rows[0][0], rows[-1][0]
            body, count = build_notice("%s:%s-%s" % (prefix, first, last), [(m, r) for _, m, r in rows])
            try:
                response = requests.post(
                    url,
                    data=body,
                    headers={"Content-Type": "application/json", SIGNATURE_HEADER: sign(body, secret)},
                    timeout=TIMEOUT,
                    allow_redirects=False,
                )
            except requests.RequestException as error:
                _logger.warning("Kitsuden did not answer a change notice: %s", error)
                return
            if response.status_code >= 300:
                _logger.warning(
                    "Kitsuden refused a change notice (%s): %s", response.status_code, response.text[:300]
                )
                return

            self.env.cr.execute("DELETE FROM kitsuden_bridge_outbox WHERE id = ANY(%s)", ([r[0] for r in rows],))
            remaining = self.search_count([])
            # Commits what was sent, and answers how long this run may still take (Odoo 18+).
            time_left = self.env["ir.cron"]._commit_progress(processed=count, remaining=remaining)
            if not remaining or time_left <= 0:
                return

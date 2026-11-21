# kitsuden-odoo-bridge

The Odoo side of [kon-odoo](https://github.com/Kitsuden/kitsuden-kons/tree/main/kons/kon-odoo): an
Odoo 19 module, `kitsuden_bridge`, that does the two things kon-odoo cannot do from outside.

**Status: tested in Odoo 19.0 Community (build 20261112).** The module's own tests pass there
(11, store credit included, with `sale_loyalty` installed), and kon-odoo's live test drives a real
Odoo with it through JSON-2: catalog, an order made by `create_order`, paid partly with store
credit, confirmed, invoiced and paid by `confirm_payment`, and the payment read back. Not yet run
in production, nor on Odoo 20.

## What it does

### 1. Tells Kitsuden what changed

When any of these change, one row is written to an outbox **in the same transaction**:

| In Odoo | Announced to Kitsuden as |
| --- | --- |
| A product (`product.template`): created, written, deleted | `product.template` |
| A variant (`product.product`) | `product.product` (a deleted variant: its template) |
| Stock (`stock.quant`): a sale at the counter, a receipt, an adjustment, a reservation | `product.product` of that quant |
| A category (`product.category`) | `product.category` |
| A sales order: created, or its state, total, lines or customer written | `sale.order` |
| A customer payment (`account.payment`, inbound): created, or its state written | `account.payment` |

A scheduled action ("Kitsuden: send changes") sends what is pending as one notice, signed with
HMAC-SHA256, to kon-odoo's `POST /api/odoo/webhook`. A change triggers it at once, so a sale at
the counter reaches the shop in seconds.

- A change Odoo rolls back is never announced: its row rolls back with it.
- Kitsuden slow or down never slows a sale: nothing is sent from inside the sale's transaction.
- A notice Kitsuden did not take stays and is sent again under the same id; Kitsuden drops a
  notice it has already processed.
- The same record changed ten times before the next send is one row.
- A notice says WHAT changed, never the new values. Kitsuden reads them back with its key.

### 2. One call, one transaction

Each call to Odoo's JSON-2 API is one database transaction. What must happen together is
therefore one method here:

| Method | What it does |
| --- | --- |
| `kitsuden.bridge.create_order(client_ref, partner_id, lines, note, confirm)` | Makes a web order with the shop's own reference; the same reference again answers the same order. |
| `kitsuden.bridge.confirm_payment(order_id, client_ref, amount, provider, reference, wallet_amount)` | Puts the store credit on the order as the eWallet program's discount line, checks that Odoo's total is what was paid, confirms the order, takes the points off the card (unless Odoo's loyalty module already did), invoices the order when it can be invoiced, and registers the payment. All or nothing. The same payment twice answers the first one's result. |

kon-odoo detects the module by asking `fields_get` on `kitsuden.bridge`, and then uses these
methods instead of its plain fallbacks.

## Install

1. Put `kitsuden_bridge/` in your Odoo addons path and install "Kitsuden Bridge". It needs Sales,
   Inventory and Invoicing. Store credit also needs the Loyalty app (`sale_loyalty`).
2. Create a user for Kitsuden. Give it **Kitsuden integration** (the group this module adds),
   Sales (all documents), Inventory (user) and Invoicing (billing). No administration. Create an
   API key for it (Preferences > Account Security); store that key in Kitsuden as the `odoo`
   connection.
3. In **Settings > Sales > Kitsuden**:
   - **Kitsuden notice address**: kon-odoo's `https://<your api>/api/odoo/webhook`.
   - **Kitsuden notice secret**: a long random string, the same one stored in kon-odoo
     (`PATCH /api/odoo/settings` with `webhookSecret`).
   - **Journal for web payments**: where card and Yappy payments are recorded. Empty: the
     company's first bank journal.
4. In Kitsuden, run kon-odoo's "Check connection": it should now say "Bridge module".

With the notice address empty, the module queues nothing and sends nothing.

## Who may call it

Only a user in the group "Kitsuden integration", or an administrator. The orders, invoices and
payments are made with that user's own rights, so Odoo's record rules and audit trail apply. Only
the module's own bookkeeping (the outbox, the payments it recorded) is written as superuser.

## Tests

The notice contract, without Odoo:

```bash
python -m unittest discover -s tests_pure -v
```

`tests_pure/test_notice.py` pins a signature that kon-odoo's tests pin too
(`tests/unit/kon-odoo.test.ts`): if either side changes how it signs, one of them fails.

The module, inside Odoo 19 (`docker-compose.yml` starts one for this, on port 8069):

```bash
docker compose up -d
docker compose exec odoo odoo -d test_kitsuden -i kitsuden_bridge --test-enable \
  --test-tags /kitsuden_bridge --stop-after-init --db_host db --db_user odoo --db_password odoo
```

## What testing against Odoo 19 found

- **Store credit is added after the order is confirmed.** Odoo's loyalty module recomputes
  eWallet lines when an order is confirmed, always to the most the card can pay: a line for 20 of
  a balance of 50 came back as 50, and the order's total dropped below what the customer had paid.
  Added to a confirmed order, the line keeps its amount and Odoo takes exactly those points.
- JSON-2 works on self-hosted Community, and a user in the integration group may call the
  abstract `kitsuden.bridge` model.
- A client must send a `Content-Length`: Odoo reads a chunked body as empty, and an empty
  `search_read` reads every field of every record (and is refused for a non-admin user).

## Not verified yet

- The settings page (`//app[@name='sale_management']`) renders; it installs, but nobody has
  opened it.
- Invoicing on delivered quantities: the payment is then recorded without an invoice, with the
  order's number in its memo. Tested only through kon-odoo's fake Odoo.
- Sending notices to a running Kitsuden: the signed notice is tested on its own and against
  kon-odoo's verifier, not end to end over HTTP.

## License

LGPL-3.0, like Odoo Community: see `LICENSE`.

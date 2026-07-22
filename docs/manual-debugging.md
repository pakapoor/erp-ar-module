# Manual Debugging Scripts

Curl-driven shell scripts in [debug/](../debug/) for exercising the running
API by hand -- invoice lifecycle, payments, GL entries, credit memos,
write-offs, voids, aging, and delivery. Complementary to the IDE
breakpoint-based guides ([START_DEBUGGER_HERE.md](../START_DEBUGGER_HERE.md),
[DEBUG_BREAKPOINTS.md](../DEBUG_BREAKPOINTS.md),
[DEBUG_INVOICE_FLOW.md](../DEBUG_INVOICE_FLOW.md)), which step through code
line by line; these scripts instead drive the API end-to-end through real
HTTP calls, the way a client would.

All scripts assume the stack is already running (`./deploy.sh` or
`./deploy.sh --no-build`) and reachable at `http://localhost:8000`.

## Session state

Every script after `1_create_invoice.sh` reads from
`debug/.debug_session`, a JSON file holding the tenant, entity, three
test users (creator / approver / payer), a customer, and the two invoice
IDs from the last create run:

```json
{
  "tenant_id": "...", "entity_id": "...",
  "user_creator_id": "...", "user_approver_id": "...", "user_payer_id": "...",
  "customer_id": "...",
  "invoice1_id": "...", "invoice2_id": "...", "last_invoice_id": "..."
}
```

Because of this, most scripts take **no required arguments** for the common
path -- they default to the invoice(s)/customer from the last
`1_create_invoice.sh` run. Pass an explicit ID only when you want to act on
something else. Run `1_create_invoice.sh` again at any point to rotate to a
completely fresh tenant/entity/customer and reset the session.

## Script reference

| # | Script | Args | No-arg default | What it does |
|---|--------|------|-----------------|--------------|
| 1 | `1_create_invoice.sh` | none | -- | Resets the session: provisions a fresh tenant/entity/users/customer, then creates 2 invoices (NET15 "Safety Valves", NET30 "Pressure Regulators"), both `DRAFT`. |
| 2 | `2_get_invoice.sh` | `[INVOICE_ID]` | both session invoices | GET with full detail: `payment_history`, `credit_memo_history`, `status_history`. |
| 3 | `3_1_patch_invoice.sh` | `[INVOICE_ID] [description] [quantity] [unit_price]` | `last_invoice_id` | Edits a `DRAFT` invoice's line items (full replace, not merge). Also the resubmit step after a rejection -- patching a `REJECTED` invoice flips it back to `DRAFT`. |
| 4 | `3_approve_invoice.sh` | `[INVOICE_ID]` | both session invoices | Approves. Posts a GL entry (`Dr AR / Cr Revenue / Cr Tax Payable`) and queues delivery to the stub. |
| 5 | `3_2_reject_invoice.sh` | `[INVOICE_ID] [reason]` | both session invoices | Rejects a `DRAFT` invoice (same `/approve` endpoint, `action: REJECT` body). Fails with `INVALID_STATUS` on anything past `DRAFT`. |
| 6 | `4_pay_invoice.sh` | `<amount>` (required) | -- | Records a payment for the **session customer** (not a single invoice) with `allocation_mode: AUTO` -- FIFO by due date across all their outstanding invoices. Can span multiple invoices in one call. |
| 7 | `5_journal_entries.sh` | `[INVOICE_ID]` | both session invoices | GL trail for an invoice: live query (uncached), shows every `INVOICE`/`PAYMENT`/`CREDIT_MEMO`/`VOID`/`WRITE_OFF` entry tied to it, each with `transaction_balanced: true` and a running `net_ar_balance` summary. |
| 8 | `6_credit_memo.sh` | `[INVOICE_ID] [AMOUNT] [REASON_CODE]` | `last_invoice_id`, amount 1000, `OVERCHARGE` | Partial credit against an `APPROVED`/`SENT` invoice. Reduces `balance_amount`; posts its own GL entry. Doesn't change invoice status. |
| 9 | `7_writeoff.sh` | `[INVOICE_ID] [REASON_CODE]` | `last_invoice_id`, `UNCOLLECTIBLE` | Writes off the full remaining balance. Status -> `WRITTEN_OFF`. Posts `Dr Bad Debt Expense / Cr AR`. |
| 10 | `8_void.sh` | `[INVOICE_ID] [REASON_CODE]` | `last_invoice_id`, `DATA_ERROR` | Voids a `DRAFT`/`APPROVED`/`SENT` invoice. From `DRAFT`, no GL entry (nothing was posted yet). From `APPROVED`/`SENT`, reverses the original GL entry (`gl_reversal: true`). Rejects `PAID`/`WRITTEN_OFF`/`VOID` invoices -- use a credit memo for those instead. |
| 11 | `9_aging.sh` | `[CUSTOMER_ID]` | session customer | AR aging report (current/30/60/90+ buckets). **Reads from a materialized view refreshed every 5 minutes by pg_cron**, not a live query -- `data_freshness` in the response tells you the snapshot age. After a payment/void/writeoff you want reflected immediately, run `refresh_aging.sh` first. |
| 12 | `refresh_aging.sh` | none | -- | Manually runs `REFRESH MATERIALIZED VIEW CONCURRENTLY ar_aging`, the same statement pg_cron runs on its 5-minute schedule. Use before `9_aging.sh` when you don't want to wait for the next cycle. |
| 13 | `tail_stub.sh` | none | -- | Streams `docker compose logs stub -f` -- live delivery events (email/EDI/IRP stub) as invoices are approved and queued for delivery. Run in a separate terminal; blocks until Ctrl+C. |

## Suggested flows

### Happy path: create -> approve -> pay -> verify GL

```bash
./debug/1_create_invoice.sh       # fresh session + 2 invoices (DRAFT)
./debug/3_approve_invoice.sh      # both -> APPROVED, GL posted, delivery queued
./debug/tail_stub.sh              # (separate terminal) watch delivery banners land
./debug/4_pay_invoice.sh 118000   # FIFO: pays invoice 1 (NET15, due first) in full
./debug/2_get_invoice.sh          # payment_history now populated
./debug/5_journal_entries.sh      # Dr AR/Cr Revenue/Cr Tax (approve) + Dr Cash/Cr AR (payment)
```

Expected: invoice 1 status `PAID`, `balance_amount: 0`; invoice 2 still
`APPROVED` with its original balance untouched, since the payment amount
only covered invoice 1.

### Reject -> edit -> reapprove

```bash
./debug/1_create_invoice.sh
./debug/3_2_reject_invoice.sh <invoice_id>          # -> REJECTED
./debug/3_1_patch_invoice.sh <invoice_id>           # edits line items, -> DRAFT
./debug/3_approve_invoice.sh <invoice_id>           # -> APPROVED
```

Expected: `version` increments at each step (1 -> 2 reject -> 3 patch -> 4
approve); the final GL entry reflects the *patched* amounts, not the
original ones.

### Partial payment, then credit memo / write-off / void

```bash
./debug/1_create_invoice.sh
./debug/3_approve_invoice.sh
./debug/4_pay_invoice.sh <partial_amount>            # partially pay first
./debug/6_credit_memo.sh <invoice_id>                # adjust remaining balance
./debug/7_writeoff.sh <invoice_id>                   # or: write off what's left
./debug/8_void.sh <invoice_id>                       # or: void (only from DRAFT/APPROVED/SENT)
./debug/refresh_aging.sh && ./debug/9_aging.sh        # confirm outstanding total updates
```

`6_credit_memo.sh`, `7_writeoff.sh`, and `8_void.sh` are alternatives for
resolving an invoice, not a sequence to run back to back on the same
invoice -- pick the one matching the scenario you're debugging.

## Gotchas

- **`4_pay_invoice.sh` targets a customer, not an invoice.** It requires an
  explicit amount (no default) and applies FIFO across everything that
  customer owes. If you want to see a payment split across two invoices in
  one allocation, pay more than the oldest invoice's balance.
- **Aging is not live.** `9_aging.sh` can lag reality by up to 5 minutes.
  If a number looks stale right after a payment/void/writeoff, run
  `refresh_aging.sh` before concluding something is broken.
- **Void has state restrictions.** Only `DRAFT`, `APPROVED`, `SENT` can be
  voided. `PAID`, `WRITTEN_OFF`, and already-`VOID` invoices are rejected
  with `INVALID_STATUS` -- use a credit memo for a paid invoice instead.
- **Rerunning `1_create_invoice.sh` rotates the whole session** (new
  tenant/entity/customer/users). Scripts that default to `last_invoice_id`
  or the session customer will silently start acting on the new session --
  old invoice IDs from a prior session return `404 Invoice not found` if
  you pass them explicitly, since they belong to a different tenant.
- **Hot reload can drop an in-flight request** if the app container is
  running with `--reload` (debug mode) and a watched file's mtime changes
  mid-request (e.g. an IDE autosave, or resuming from a debugger pause).
  The symptom is an empty response body / JSON decode error on one of the
  two calls in a script that fires two requests back to back
  (`1_create_invoice.sh`'s second invoice, in particular). Rerun the
  script; it's an artifact of the reload racing the request, not a bug in
  the script or API.

## Inspecting the database directly

Scripts drive the API, but sometimes you want ground truth from Postgres,
bypassing the app and materialized-view caching entirely:

```bash
docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT id, status, total_amount, balance_amount FROM invoice WHERE customer_id = '<customer_id>';"

docker compose exec -T db psql -U erp_user -d erp_db -c \
  "SELECT customer_id, as_of, total_outstanding FROM ar_aging WHERE customer_id = '<customer_id>';"
```

Comparing the two above is exactly how you confirm whether `9_aging.sh`
is showing you a stale materialized-view snapshot or the current DB state.

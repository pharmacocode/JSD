# JSD Group — COGS & Inventory Management App

Mobile-first web app for JSD Group (custom bottle-labeling agency): raw-material
FIFO inventory, client-specific label stock, production-free direct COGS with
FIFO batch costing, client ledgers, overhead/labour allocation, and
sales/inventory dashboards. Currency: INR (₹). No auth, no GST.

## Architecture (confirmed hosting split)

| Layer    | Tech                                | Hosting    |
|----------|-------------------------------------|------------|
| Frontend | Vue 3 + Vite + Vuetify + Pinia      | **Netlify** |
| Backend  | Django + DRF (JSON API)             | **Render** (or Railway) |
| Database | PostgreSQL via Django ORM           | **Supabase** (direct Postgres connection string — no Supabase SDK) |

Netlify cannot run a persistent Django/WSGI process, hence the split. The Vue
app calls the Render-hosted API through `VITE_API_BASE_URL`.

## Repo layout

```
backend/           Django project (jsd/) + app (core/)
  core/models.py   Data model (spec Section 3) + audit rules (3.7)
  core/cogs.py     FIFO engine + COGS calculation (spec Sections 5–6)
  core/tests.py    Unit tests: FIFO order, frozen COGS, shortfall, ledger…
  core/urls.py     /api/ routes
frontend/          Vue 3 SPA (Vuetify, mobile-first, bottom nav)
  src/views/       Home, Add Delivery, Arrived Stock, Stock Adjustment,
                   Clients, Client detail/form, Masters (MVP/SKU/Overheads),
                   Reports
```

## Local development

### Backend

```bash
cd backend
python -m pip install -r requirements.txt
python manage.py migrate          # seeds Rent/Diesel/Electricity/Labour
python manage.py runserver        # http://localhost:8000
python manage.py test core        # 57 tests (FIFO + COGS + ledger + markers + home)
```

Without `DATABASE_URL` the backend uses local SQLite (dev/tests). Set it to
connect to Supabase Postgres.

### Frontend

```bash
cd frontend
npm install
cp .env.example .env              # VITE_API_BASE_URL=http://localhost:8000
npm run dev                       # http://localhost:5173
npm run build                     # production build -> dist/
```

## Production wiring

### 1. Supabase (database)
Create a project → copy the **Postgres connection string** (URI mode, port 5432):

```
postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres
```

### 2. Render (backend)
New **Web Service** → root `backend/`:

- Build command: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
- Start command: `gunicorn jsd.wsgi`
- Env vars:
  - `DATABASE_URL` = Supabase connection string (ssl enforced by default; set `DB_SSL_REQUIRE=False` only if needed)
  - `DJANGO_DEBUG=False`
  - `DJANGO_ALLOWED_HOSTS=<your-render-domain>.onrender.com`
  - `DJANGO_SECRET_KEY` = long random string
  - `CORS_ALLOWED_ORIGINS=https://<your-site>.netlify.app`

Migrations run on every deploy (safe: all migrations are additive/reversible).

### 3. Netlify (frontend)
Publish directory `frontend/dist` (netlify.toml + `_redirects` included for
SPA routing). Env var:

- `VITE_API_BASE_URL=https://<your-render-domain>.onrender.com`

## Key business rules (implemented as specified)

- **Everything is per CASE** (bottles are never considered anywhere): material
  receipts, stock in hand, SKU requirements (`qty_per_case`), print cost,
  overhead allocation and COGS are all expressed per case.
- **FIFO batch costing (spec 5):** consumption walks `MaterialBatch` rows
  oldest-first; the delivery's COGS is the weighted cost of the batches
  actually consumed — cost follows the physical batch, not the calendar month.
- **Live-overhead COGS:** `StockDelivery.base_cogs_per_case_snapshot` freezes only
  the direct cost (FIFO materials + print). The displayed per-case COGS is
  `base + <delivery month's current overhead per case>`, so later overhead /
  labour edits for that month update every delivery of the month.
  `cogs_per_case_snapshot` keeps the full figure as at creation (audit only).
- **Ledger is the single source of truth:** client pending balance is always a
  live sum of `ClientLedgerEntry.amount` (positive = delivery, negative =
  payment), plus the optional *balance marker* described next. No running total
  is ever stored.
- **Balance markers ("pending amount as of a date", user request):** the client's
  pending amount and the vendor's payable can be **edited at any time together
  with the date they refer to** (`Client.pending_as_of_amount` /
  `pending_as_of_date`, `Vendor.payable_as_of_amount` / `payable_as_of_date`).
  The entered figure is what was owed **on** that date: ledger entries, arrived
  stock and payments dated **strictly after** it are added on top, while
  everything dated on or before it is already inside the figure — those rows are
  flagged *superseded* in the ledger (`is_superseded`, no running balance) so the
  same transaction is never counted twice. API: `GET|POST|DELETE
  /api/clients/<id>/pending/` and `GET|POST|DELETE /api/vendors/<id>/payable/`;
  the `GET` accepts `?as_of=YYYY-MM-DD&amount=X` to preview a candidate marker
  without saving, and `DELETE` clears it (balance returns to the plain
  transaction sum).
- **Audit (3.7):** batches, deliveries, ledger entries, and adjustments are
  soft-deleted (`is_deleted`) with `edit_history` JSON; UI shows
  Edited/Deleted badges with expandable history.
- **Stock adjustments (4.4):** distinct `StockAdjustment` records with a
  mandatory reason — positive adds a zero-price batch, negative consumes the
  FIFO queue. The FIFO engine never rewrites what was keyed; a **wrongly keyed
  arrival is corrected on the Home screen** instead (see the next bullet).
- **Negative stock is visible, never silently ignored (user request):** a
  delivery recorded without enough stock (409 → *Proceed anyway*) always
  drives `stock_in_hand` **negative** — partially consumed batches go
  negative, an exhausted queue is pushed further negative on its oldest
  batch, and a material with no batches at all gets a zero-received deficit
  *carrier* batch (costed at the master price, so COGS still matches the
  Add-Delivery preview; carriers are excluded from the inward list). Home's
  *Stock in Hand* marks such materials **SHORT** with an `<n> negative`
  count, and the deficit clears either via a **Stock Adjustment** or a
  **backdated (retrospective) arrival** on *Enter Arrived Stock*.
- **Inward line items are editable (user request):** the Home screen lists the
  month's arrivals with their underlying batches; each line can be edited in
  place (received cases, landing price, arrival date) or removed. Editing
  `quantity_received` shifts `quantity_remaining` by the **same delta**, so
  already-consumed cases stay consumed and stock in hand, the FIFO queue and the
  vendor's payable immediately follow the corrected figure (`PATCH
  /api/batches/<id>/`, audited in `edit_history`). Shrinking a batch below what
  was already consumed drives the remainder negative — the same convention the
  FIFO shortfall path uses — so the difference stays visible and can be
  reconciled with a Stock Adjustment. A corrected price/date feeds FIFO from
  then on; deliveries already made keep the direct cost frozen at their
  creation (`base_cogs_per_case_snapshot`).
- **Home summary (user request):** *Stock in Hand* always shows the red
  `<n> low` / green `<n> above alert` counts and expands to the per-material
  detail when tapped. The monthly card is titled **`Summary — MMM, YYYY`** and
  its tiles — **Cases sold · Revenue · Profit · Overhead** — are clickable: the
  single chart area below is populated by the selected tile, i.e. cases per SKU
  (default), revenue per client (highest → lowest) or profit per client, with an
  **Include overhead** toggle for the profit view. Revenue/profit come from
  `stats.client_breakdown` (= revenue − frozen direct cost − the client's share
  of the month's per-case overhead); the profit tile follows the toggle so card
  and bars always agree. The old "top clients by revenue" doughnut is gone.
- **Chart drill-downs (user request):** tapping a bar opens a break-up below
  the chart — on *Cases sold* the picked SKU shows the **clients served**
  (bar chart); on *Revenue* the picked client shows the **case count per SKU
  delivered that month**, with per-SKU revenue adding back up to the client's
  bar; on *Profit* the picked client shows **profit per SKU**, and tapping a
  SKU shows the full cost chain (revenue → raw materials → print → the month's
  overhead per category → profit). The rows come from
  `stats.sku_client_matrix` (+ `stats.overhead_categories`) and sum exactly to
  the bars above; selections clear on metric/month change.
- **Employees are editable (user request):** `Employee` rows (name, role,
  monthly pay, active) can be edited at any time from Masters → Overheads &
  Labour (one dialog handles add *and* edit). Retiring someone = setting
  **Active = off** — they stay in the list so their logged payments keep their
  month's Labour roll-up intact.
- **Overhead allocation (6.3):** `month overhead ÷ cases sold that month`
  (including the delivery being created) = overhead per case directly.
  Labour auto-sums `EmployeePayment` rows into the Labour category (manual
  override respected).
- **Vendors:** the material form picks a vendor from the Vendor master;
  a vendor's **amount owed = value of arrived stock from that vendor's
  materials − payments recorded to the vendor** — or, when a payable marker is
  set, that figure plus every purchase/payment after its date.

### Assumptions confirmed with the user (v1)

1. Overhead-per-bottle is a **live-recomputing estimate** through the month
   (no "close month" lock). Historical COGS is unaffected because snapshots
   are frozen at delivery creation.
2. Stock adjustments are **distinct records** layered on the FIFO queue.
3. `selling_price_per_case` **remains overridable per delivery** (auto-filled
   from the client master by default).

### Out of scope for v1 (per spec Section 9)

Auth/roles · GST · discounts (override exists in the data model only) ·
printable invoices · embedded maps (link-out only) · CSV/Excel export · P&L.

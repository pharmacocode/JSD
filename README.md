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
python manage.py test core        # 27 tests (FIFO + COGS + API)
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
  payment). Nothing is stored mutably on `Client`.
- **Audit (3.7):** batches, deliveries, ledger entries, and adjustments are
  soft-deleted (`is_deleted`) with `edit_history` JSON; UI shows
  Edited/Deleted badges with expandable history.
- **Stock adjustments (4.4):** distinct `StockAdjustment` records with a
  mandatory reason — positive adds a zero-price batch, negative consumes the
  FIFO queue. Existing batches are never silently edited.
- **Overhead allocation (6.3):** `month overhead ÷ cases sold that month`
  (including the delivery being created) = overhead per case directly.
  Labour auto-sums `EmployeePayment` rows into the Labour category (manual
  override respected).
- **Vendors:** the material form picks a vendor from the Vendor master;
  a vendor's **amount owed = value of arrived stock from that vendor's
  materials − payments recorded to the vendor**.

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

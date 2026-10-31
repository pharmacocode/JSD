<script setup>
/**
 * Home (spec 4.1, reworked per user request): FAB for + Add Delivery, secondary
 * + Enter Arrived Stock, a collapsible Assets panel (Expected / Received /
 * Pending / Vendor dues) just above Stock in Hand whose tiles drive their own
 * bar chart, a Stock in Hand panel that always shows the low /
 * healthy summary and expands on click, and a "Summary — MMM, YYYY" card whose
 * tiles (cases sold / revenue / profit / overhead) drive ONE chart area.
 * The month's inward material arrivals sit at the bottom with editable line
 * items — correcting one recalculates stock, FIFO cost and the payable.
 *
 * The order pipeline (Enter Order → Pending Orders → Ready to Deliver) has been
 * removed from this screen per user request: a delivery is now entered directly
 * against the client on /delivery/new, which is the only path stock leaves by.
 */
import { ref, watch, onMounted, onActivated, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api, listify } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, num, monthLong, rangeLong } from '@/utils/format'
import MonthPicker from '@/components/MonthPicker.vue'
import ChartCanvas from '@/components/ChartCanvas.vue'
import EmptyState from '@/components/EmptyState.vue'
import ExportDialog from '@/components/ExportDialog.vue'
import { exportDeliveryRegister } from '@/utils/pdf'

const router = useRouter()
const ui = useUiStore()
const loading = ref(true)
const data = ref({ stock: [], stats: {} })
const error = ref('')

// Panel / chart state.
const stockOpen = ref(false)
const metric = ref('cases') // cases | revenue | profit — drives the chart area
const includeOverhead = ref(true) // profit toggle (user request)
// Each of the four panels on this screen collapses independently, in the same
// way (user request: "all these views are expandable and collapsible").
const deliveriesOpen = ref(false)
const inwardPanelOpen = ref(false)
const inwardDraft = ref({}) // batch id -> editable working copy
const inwardSaving = ref(null)

// Delivery register: recent on top (user request). The API returns the rows
// oldest-first with entry order as the tie-break, so the client just reverses
// them — the totals above do not depend on order.
const deliveryRows = computed(() => [...(stats.value.deliveries || [])].reverse())

const deliveryTotals = computed(() => {
  const rows = deliveryRows.value
  return {
    count: rows.length,
    cases: rows.reduce((s, r) => s + Number(r.qty_cases), 0),
    amount: rows.reduce((s, r) => s + Number(r.total_amount), 0),
  }
})

// ---------------------------------------------------------------------------
// Delivery register Export PDF (user request): an arbitrary date range, rows
// fetched from /deliveries/ (they carry live per-case costs) and rendered in
// utils/pdf.js. The profit toggle on this screen flows into the PDF.
// ---------------------------------------------------------------------------
const exportOpen = ref(false)

// Default range = the period the register on screen is showing.
const exportDefaults = computed(() => {
  if (ui.rangeActive) return { from: ui.rangeFrom, to: ui.rangeTo }
  const [y, m] = ui.month.split('-').map(Number)
  const last = new Date(y, m, 0).getDate()
  const pad = (n) => String(n).padStart(2, '0')
  return { from: `${ui.month}-01`, to: `${ui.month}-${pad(last)}` }
})

// Preview columns of the export dialog (the PDF renders more: revenue+profit).
const registerColumns = [
  { key: 'date', label: 'Date' },
  { key: 'client_name', label: 'Client' },
  { key: 'sku_description', label: 'SKU' },
  { key: 'qty_cases', label: 'Qty', align: 'right' },
  {
    key: 'total_amount',
    label: 'Revenue',
    align: 'right',
    format: (v) => money(v),
  },
]

/** ExportDialog row source: the server scopes rows to the chosen range. */
async function loadExportRows({ from, to }) {
  // /deliveries/ is DRF-paginated, so the { results } payload is unwrapped
  // the same way every other list in this app is.
  return listify(await api.get('/deliveries/', { from, to }))
}

async function runExport({ from, to, rows }) {
  try {
    // Profit per row from the server's blended figures: /deliveries/
    // computes them the same way the Home tile does — one FIFO walk over the
    // whole filtered set (cheap stock is never double-counted) plus each
    // delivery's own-month overhead share — so the PDF total always matches
    // the tile to the paise (user bug report: they disagreed). The per-case
    // columns stay as a fallback if an older payload lacks the fields.
    const withProfit = rows.map((r) => {
      const server = includeOverhead.value
        ? r.profit_incl_overhead
        : r.profit_excl_overhead
      const fallback =
        Number(r.total_amount) -
        Number(r.qty_cases) *
          Number(
            includeOverhead.value
              ? r.cogs_per_case_current
              : r.base_cogs_per_case,
          )
      return {
        ...r,
        _profit:
          server !== undefined && server !== null ? Number(server) : fallback,
      }
    })
    const file = await exportDeliveryRegister({
      from,
      to,
      rows: withProfit,
      profitLabel: includeOverhead.value ? 'Profit after OH' : 'Profit before OH',
      profitNote: includeOverhead.value
        ? "Profit = revenue - direct cost (FIFO materials + print) - this period's overhead per case (live)."
        : 'Profit = revenue - direct cost (FIFO materials + print), before overhead.',
    })
    ui.notify(`Delivery register exported — ${file}`)
  } catch (e) {
    ui.notify(e?.message || 'Export failed', 'error')
  }
}

// Query params for the selected period (user request): a complete from/to
// range drives the dashboard and the month is ignored entirely; anything else
// falls back to ?month=, so the month picker keeps working exactly as before.
function periodParams() {
  if (ui.rangeActive) return { from: ui.rangeFrom, to: ui.rangeTo }
  return { month: ui.month }
}

// Heading for whatever period is selected — 'Oct, 2026' or '1 Jan 2026 → …'.
const periodLabel = computed(() =>
  ui.rangeActive ? rangeLong(ui.rangeFrom, ui.rangeTo) : monthLong(ui.month)
)

async function load() {
  // Perf plan 2.9: the dashboard is cached per period, so coming back to Home
  // from another screen paints instantly and only refreshes in the background.
  const cached = ui.getDashboard()
  if (cached) {
    data.value = cached
    loading.value = false
    error.value = ''
    refresh()
    return
  }
  loading.value = true
  error.value = ''
  await refresh()
}

// One in-flight dashboard request per period: a <keep-alive>d view fires
// onMounted *and* onActivated on its first mount (perf plan 2.10), so without
// this the dashboard would be fetched twice for the same period.
const inflight = {}
async function refresh() {
  const key = ui.periodKey
  const params = periodParams()
  if (inflight[key]) return inflight[key]
  const req = (async () => {
    try {
      const payload = await api.get('/dashboard/', params)
      // Ignore a response that lands after the period changed again.
      if (key !== ui.periodKey) return
      data.value = payload
      ui.setDashboard(key, payload)
    } catch (e) {
      error.value = e.message
    } finally {
      loading.value = false
      delete inflight[key]
    }
  })()
  inflight[key] = req
  return req
}

onMounted(loadAll)
// Revived from the <keep-alive> cache: repaint from the cached dashboard and
// revalidate in the background so Home is never stale after a delivery.
onActivated(loadAll)
// periodKey covers the month, the mode and both dates in one key, so this
// single watcher fires for every way the period can change.
watch(() => ui.periodKey, loadAll)

const stats = computed(() => data.value.stats || {})
const stock = computed(() => data.value.stock || [])
const belowAlert = computed(() => stock.value.filter((s) => s.below_alert))
const aboveAlert = computed(() => stock.value.filter((s) => !s.below_alert))
// Negative stock = promised beyond what was in hand (user request): counted
// separately so the user Stock-Adjusts or backdates an arrival to clear it.
// Measured on AVAILABLE, matching the tiles and the backend's alert flag, so
// the badge and the red tiles always count the same materials.
const negativeStock = computed(() =>
  stock.value.filter((s) => Number(s.available) < 0)
)

// The profit tile follows the chart toggle, so tile and bars always agree.
const profitTotal = computed(() =>
  includeOverhead.value
    ? stats.value.total_profit_incl_overhead
    : stats.value.total_profit_excl_overhead
)

const profitKey = computed(() =>
  includeOverhead.value ? 'profit_incl_overhead' : 'profit_excl_overhead'
)

// Summary tiles — the first three drive the chart area, Overhead is reference.
const tiles = computed(() => {
  const base = [
    {
      key: 'cases',
      label: 'Cases sold',
      color: 'primary',
      value: num(stats.value.total_cases, 0),
    },
    {
      key: 'revenue',
      label: 'Revenue',
      color: 'success',
      value: money(stats.value.total_revenue),
    },
    {
      key: 'profit',
      label: 'Profit',
      color: 'teal',
      value: money(profitTotal.value),
      note: includeOverhead.value ? 'after overhead' : 'before overhead',
    },
    {
      key: 'overhead',
      label: 'Overhead',
      color: 'warning',
      value: money(stats.value.total_overhead),
      note: `${money(stats.value.overhead_per_case)} / case`,
      selectable: false,
    },
  ]
  return base.map((t) => ({
    ...t,
    selectable: t.selectable !== false,
    caption: t.selectable === false ? 'total for the month'
      : metric.value === t.key ? 'chart below' : 'tap for chart',
  }))
})

// ONE chart area, populated by whatever was selected above.
const chart = computed(() => {
  if (metric.value === 'cases') {
    const rows = stats.value.cases_sold_per_sku || []
    return {
      title: 'Cases sold per SKU',
      subtitle:
        rows.length === 1
          ? '1 SKU sold this month'
          : `${rows.length} SKUs sold this month`,
      labels: rows.map((r) => r.sku),
      keys: rows.map((r) => r.sku_id),
      data: rows.map((r) => Number(r.cases)),
      label: 'Cases',
      empty: 'No sales this month yet.',
    }
  }
  const key = metric.value === 'profit' ? profitKey.value : 'revenue'
  const rows = [...(stats.value.client_breakdown || [])].sort(
    (a, b) => Number(b[key]) - Number(a[key])
  )
  const isProfit = metric.value === 'profit'
  return {
    // Short label + qualifier line, instead of one long mixed-size title.
    title: isProfit ? 'Profit per client' : 'Revenue per client',
    subtitle: isProfit
      ? `${includeOverhead.value ? 'After' : 'Before'} overhead · highest first`
      : 'Highest first',
    labels: rows.map((r) => r.client),
    keys: rows.map((r) => r.client_id),
    data: rows.map((r) => Number(r[key])),
    label: isProfit ? 'Profit (₹)' : 'Revenue (₹)',
    empty: 'No client figures this month.',
  }
})

// --- Chart drill-downs (user request) ---------------------------------------
// Cases chart: tap a SKU -> clients served for that SKU (bar chart).
// Revenue chart: tap a client -> the SKUs delivered to them this month as
// case counts, with per-SKU revenue that adds back up to the client's bar.
// Profit chart: tap a client -> profit per SKU, then tap a SKU -> the cost
// chain (revenue - raw materials - print - overhead share = profit).
const drillSku = ref(null) // cases chart: selected sku_id
const drillClient = ref(null) // revenue / profit charts: selected client_id
const drillProfitSku = ref(null) // profit drill: selected sku_id

const matrix = computed(() => stats.value.sku_client_matrix || [])

function onMainSelect(index) {
  const id = chart.value.keys?.[index]
  if (id == null) return
  if (metric.value === 'cases') {
    drillSku.value = drillSku.value === id ? null : id
  } else {
    drillClient.value = drillClient.value === id ? null : id
    drillProfitSku.value = null
  }
}

function onProfitDrillSelect(index) {
  const row = profitDrill.value?.rows?.[index]
  if (!row) return
  drillProfitSku.value = drillProfitSku.value === row.sku_id ? null : row.sku_id
}

// Drill-downs belong to one metric and one month — clear them when either
// changes so a stale selection can never point at the wrong rows.
watch([metric, () => ui.periodKey], () => {
  drillSku.value = null
  drillClient.value = null
  drillProfitSku.value = null
})

// Cases chart drill: clients served for the picked SKU, highest first.
const casesDrill = computed(() => {
  const skuId = drillSku.value
  const rows = matrix.value
    .filter((r) => r.sku_id === skuId)
    .sort((a, b) => Number(b.cases) - Number(a.cases))
  if (!rows.length) return null
  const totalCases = rows.reduce((s, r) => s + Number(r.cases), 0)
  return {
    title: `Clients served — ${rows[0].sku}`,
    subtitle: `${periodLabel.value} · ${num(totalCases, 0)} cases sold`,
    labels: rows.map((r) => r.client),
    data: rows.map((r) => Number(r.cases)),
    label: 'Cases',
  }
})

// The revenue / profit bar of the client whose bar was tapped.
const clientBar = computed(() =>
  (stats.value.client_breakdown || []).find(
    (r) => r.client_id === drillClient.value
  )
)

// Revenue chart drill: SKUs delivered to that client this month (counts).
// The per-SKU revenue rows sum to exactly the revenue bar above — both come
// from the same deliveries of the same month.
const revenueDrill = computed(() => {
  const bar = clientBar.value
  if (!bar) return null
  const rows = matrix.value
    .filter((r) => r.client_id === bar.client_id)
    .sort((a, b) => Number(b.revenue) - Number(a.revenue))
  if (!rows.length) return null
  return {
    title: `SKUs delivered — ${bar.client}`,
    subtitle: `${periodLabel.value} · case counts, revenue adds up to the bar`,
    labels: rows.map((r) => r.sku),
    data: rows.map((r) => Number(r.cases)),
    label: 'Cases',
    rows,
    totalRevenue: bar.revenue,
  }
})

// Profit chart drill: profit per SKU for that client, highest first.
const profitDrill = computed(() => {
  const bar = clientBar.value
  if (!bar) return null
  const rows = matrix.value
    .filter((r) => r.client_id === bar.client_id)
    .sort((a, b) => Number(b[profitKey.value]) - Number(a[profitKey.value]))
  if (!rows.length) return null
  return {
    title: `Profit per SKU — ${bar.client}`,
    subtitle: `${periodLabel.value} · ${
      includeOverhead.value ? 'after' : 'before'
    } overhead · tap a bar for the cost breakup`,
    labels: rows.map((r) => r.sku),
    data: rows.map((r) => Number(r[profitKey.value])),
    label: 'Profit (₹)',
    rows,
  }
})

// Cost chain for the tapped client x SKU: revenue -> raw materials -> print
// -> overhead share (per category) -> profit. The figures are the backend's
// exact 2dp strings, so the chain adds up to the bars above to the paise.
const profitSkuDetail = computed(() => {
  const row = matrix.value.find(
    (r) =>
      r.client_id === drillClient.value && r.sku_id === drillProfitSku.value
  )
  if (!row) return null
  const cats = stats.value.overhead_categories || []
  const cases = Number(row.cases)
  const sold = Number(stats.value.total_cases) || 0
  // Per-category share of this line, remainder-adjusted so the categories
  // add up exactly to the row's overhead share.
  const rounded = cats.map((c) =>
    Math.round((sold ? (cases * Number(c.amount)) / sold : 0) * 100) / 100
  )
  const delta =
    Math.round(
      (Number(row.overhead) - rounded.reduce((a, b) => a + b, 0)) * 100
    ) / 100
  if (rounded.length) {
    const i = rounded.indexOf(Math.max(...rounded))
    rounded[i] = Math.round((rounded[i] + delta) * 100) / 100
  }
  return {
    row,
    categories: cats.map((c, i) => ({
      category: c.category,
      month: Number(c.amount),
      share: rounded[i],
    })),
  }
})
// ---------------------------------------------------------------------------
// Assets panel (user request): a collapsible card just above Stock in Hand
// whose tiles — Expected (the period's revenue) / Received (payments recorded
// in the period) / Pending (lifetime client balances) / Vendor dues (lifetime
// payable) — drive their own bar chart, the same way the Summary tiles do.
// The figures come from `stats.assets`; the period is whatever the picker at
// the top has selected (month OR explicit from/to range).
// ---------------------------------------------------------------------------
const assetsOpen = ref(false)
const assetMetric = ref('expected') // expected | received | pending | vendor

const assets = computed(() => stats.value.assets || {})

const assetTiles = computed(() => {
  const a = assets.value
  const base = [
    {
      key: 'expected',
      label: 'Expected',
      color: 'success',
      value: money(a.expected),
      note: 'revenue for the period',
    },
    {
      key: 'received',
      label: 'Received',
      color: 'primary',
      value: money(a.received),
      note: `payments in ${periodLabel.value}`,
    },
    {
      key: 'pending',
      label: 'Pending',
      color: 'warning',
      value: money(a.pending),
      note: 'from clients, lifetime',
    },
    {
      key: 'vendor',
      label: 'Vendor dues',
      color: 'error',
      value: money(a.vendor_payable),
      note: 'we owe, lifetime',
    },
  ]
  return base.map((t) => ({
    ...t,
    caption: assetMetric.value === t.key ? 'chart below' : 'tap for chart',
  }))
})

// Highest first so the biggest bar always reads left to right.
function byAmountDesc(rows) {
  return [...rows].sort((x, y) => Number(y.amount) - Number(x.amount))
}

// The Assets chart area: the per-client / per-vendor rows of whichever tile
// is selected above.
const assetChart = computed(() => {
  const a = assets.value
  if (assetMetric.value === 'received') {
    const rows = byAmountDesc(a.received_per_client || [])
    return {
      title: 'Received per client',
      subtitle: `${periodLabel.value} · payments recorded in the period`,
      labels: rows.map((r) => r.client),
      data: rows.map((r) => Number(r.amount)),
      label: 'Received (₹)',
      empty: 'No payments received in this period.',
    }
  }
  if (assetMetric.value === 'pending') {
    // Zero balances carry no information in a bar chart — drop them.
    const rows = byAmountDesc(
      (a.pending_per_client || []).filter((r) => Number(r.amount) !== 0)
    )
    return {
      title: 'Pending per client',
      subtitle: 'Lifetime balances · tap a client in Clients for the ledger',
      labels: rows.map((r) => r.client),
      data: rows.map((r) => Number(r.amount)),
      label: 'Pending (₹)',
      empty: 'Nothing pending from any client.',
    }
  }
  if (assetMetric.value === 'vendor') {
    const rows = byAmountDesc(
      (a.vendor_payable_per_vendor || []).filter((r) => Number(r.amount) !== 0)
    )
    return {
      title: 'Payable per vendor',
      subtitle: 'Lifetime · what we still owe each vendor',
      labels: rows.map((r) => r.vendor),
      data: rows.map((r) => Number(r.amount)),
      label: 'We owe (₹)',
      empty: 'Nothing owed to any vendor.',
    }
  }
  const rows = a.expected_per_client || []
  return {
    title: 'Expected per client',
    subtitle: `${periodLabel.value} · revenue of the period's deliveries`,
    labels: rows.map((r) => r.client),
    data: rows.map((r) => Number(r.amount)),
    label: 'Expected (₹)',
    empty: 'No deliveries in this period.',
  }
})

// --- Inward material line items (editable at any time) ---------------------
// The inward register is shown as a flat, chronological table (date /
// material / qty) rather than grouped rows, so it reads like a register book
// (user request). `inwardRows` flattens the API's per-material rows into one
// line per batch, already sorted oldest-first.
const inwardRows = computed(() => {
  const rows = []
  for (const group of stats.value.inward_materials || []) {
    for (const item of group.items || []) {
      rows.push({
        ...item,
        material_id: group.material_id,
        material: group.material,
        unit: group.unit,
        // The API nests two different "consumed" numbers: the BATCH's own
        // consumption (this item) and the MATERIAL's consumption for the month
        // (the group). Keep both, under names that cannot collide.
        batch_consumed: item.consumed,
        month_consumed: group.consumed,
        // `available` is THIS line's own balance — what is still left of this
        // one arrival. It is deliberately NOT the material total: repeating the
        // material total on every line made "In hand" read 1,120 on all four
        // 1 L rows (user bug report). The material figure now travels as
        // `material_stock`, and the register shows it once, in the tiles.
        available: item.quantity_remaining,
        material_stock: group.stock_in_hand,
      })
    }
  }
  return rows.sort((a, b) => (a.date === b.date ? a.id - b.id : a.date < b.date ? -1 : 1))
})

// Material filter chips above the table. `null` = show every material; picking
// a chip narrows the register to that one. Empty set is impossible by design,
// so "All" always has something to show.
const inwardFilter = ref(null)

const inwardMaterials = computed(() => {
  const seen = []
  for (const row of inwardRows.value) {
    if (!seen.some((m) => m.id === row.material_id)) {
      seen.push({ id: row.material_id, name: row.material })
    }
  }
  return seen.sort((a, b) => a.name.localeCompare(b.name))
})

const visibleInwardRows = computed(() =>
  inwardFilter.value === null
    ? inwardRows.value
    : inwardRows.value.filter((r) => r.material_id === inwardFilter.value)
)

const inwardTotal = computed(() =>
  visibleInwardRows.value.reduce((sum, r) => sum + Number(r.quantity_received), 0)
)

// Month reconciliation for the inward register (user request): the four figures
// asked for, scoped to whatever the filter currently shows, tying out as
//   carry forward + inward - delivered = in hand.
// "In hand" is the material's live balance, so this holds even when the cases
// arrived in an earlier month.
//
// `month_consumed` and `material_stock` are MATERIAL-level figures that the API
// repeats on every arrival line of that material, so they must be counted once
// per material, not once per line (user bug report: 1,120 in hand on four 1 L
// rows showed up as 4,480 in the tile). `inward` is a genuine per-line figure
// and stays a plain sum.
const inwardSummary = computed(() => {
  const rows = visibleInwardRows.value
  const inward = rows.reduce((s, r) => s + Number(r.quantity_received), 0)
  const perMaterial = new Map()
  for (const row of rows) {
    if (!perMaterial.has(row.material_id)) {
      perMaterial.set(row.material_id, {
        consumed: Number(row.month_consumed) || 0,
        inHand: Number(row.material_stock) || 0,
      })
    }
  }
  const materials = [...perMaterial.values()]
  const delivered = materials.reduce((s, m) => s + m.consumed, 0)
  const inHand = materials.reduce((s, m) => s + m.inHand, 0)
  return { inward, delivered, inHand, carryForward: inHand + delivered - inward }
})

// Footer totals for the register table: what arrived on the visible lines, how
// much of it the FIFO queue has already taken and what is left of it. All three
// are per-line figures, so they sum cleanly (user request: date / received /
// consumed / available).
const inwardLineTotals = computed(() => {
  const rows = visibleInwardRows.value
  return {
    received: rows.reduce((s, r) => s + Number(r.quantity_received), 0),
    consumed: rows.reduce((s, r) => s + Number(r.batch_consumed), 0),
    available: rows.reduce((s, r) => s + Number(r.available), 0),
  }
})

// The reconciliation as four tiles (user request). Negative figures are shown in
// red so a material that was over-delivered still stands out.
const inwardSummaryTiles = computed(() => {
  const s = inwardSummary.value
  const tone = (n) => (n < 0 ? 'text-error' : '')
  return [
    { label: 'Inward', value: num(s.inward), tone: '' },
    { label: 'Delivered', value: num(s.delivered), tone: '' },
    { label: 'Carry forward', value: num(s.carryForward), tone: tone(s.carryForward) },
    { label: 'In hand', value: num(s.inHand), tone: tone(s.inHand) },
  ]
})

function startEdit(item) {
  inwardDraft.value = {
    ...inwardDraft.value,
    [item.id]: {
      quantity_received: Number(item.quantity_received),
      price_per_unit: Number(item.price_per_unit),
      arrival_date: item.date,
    },
  }
}

function cancelEdit(id) {
  const next = { ...inwardDraft.value }
  delete next[id]
  inwardDraft.value = next
}

async function saveItem(item) {
  const draft = inwardDraft.value[item.id]
  if (!draft) return
  const raw = inwardDraft.value[item.id].quantity_received
  // An empty box is NOT 0 — clearing the field must never silently cancel an
  // arrival, because 0 is a deliberate correction here.
  const qty =
    raw === '' || raw === null || raw === undefined ? NaN : Number(raw)
  if (!Number.isFinite(qty)) {
    error.value =
      'Received must be a number — enter 0 to cancel an arrival (the row ' +
      'stays in the history with nothing in hand).'
    return
  }
  inwardSaving.value = item.id
  error.value = ''
  try {
    await api.patch(`/batches/${item.id}/`, {
      quantity_received: String(qty),
      price_per_unit: String(draft.price_per_unit),
      arrival_date: draft.arrival_date,
    })
    cancelEdit(item.id)
    await load() // stock in hand, FIFO cost, payable and totals recalculated
    ui.notify('Inward line updated — figures recalculated')
  } catch (e) {
    error.value = e.message
  } finally {
    inwardSaving.value = null
  }
}

/**
 * Deleting an arrival is guarded in the API: a row that has already fed
 * deliveries must not disappear, because those cases stay booked while the row
 * they came from leaves the register — re-keying the arrival afterwards is what
 * overstated stock in October 2026 (1,104 units across three materials). So for
 * a consumed row this pivots straight to the edit form, where correcting
 * Received (0 allowed) is the safe, audited way to cancel it.
 */
async function deleteItem(item) {
  const consumed = Number(item.batch_consumed) || 0
  if (consumed > 0) {
    error.value =
      `Arrival #${item.id} has already fed ${num(consumed)}, so it cannot be ` +
      'deleted — those cases would stay booked against a row the register no ' +
      'longer shows. Correct it instead: set Received to what actually ' +
      'arrived (0 cancels the arrival and keeps the history).'
    startEdit(item)
    return
  }
  const ok = confirm(
    `Remove this arrival of ${item.quantity_received}? Stock in hand and the ` +
      'vendor payable are recalculated.'
  )
  if (!ok) return
  inwardSaving.value = item.id
  error.value = ''
  try {
    await api.del(`/batches/${item.id}/`)
    cancelEdit(item.id)
    await load()
    ui.notify('Arrival removed — figures recalculated')
  } catch (e) {
    // 409 = the API refused it because deliveries already drew on this row; its
    // message names the quantity and the two safe ways forward.
    error.value = e.message
  } finally {
    inwardSaving.value = null
  }
}

// ---------------------------------------------------------------------------
// Dashboard refresh
// ---------------------------------------------------------------------------
// The order pipeline (Enter Order / Pending / Ready to Deliver) used to be read
// here as well, because it committed stock and the tiles show what is free.
// Deliveries are now keyed directly against the client (user request), so there
// is no commitment to re-read: the dashboard and the net-worth tiles suffice.
const _load = load
async function loadAll() {
  // Both requests in one round of parallel work: they are independent, and on
  // a slow link waiting for the (larger) dashboard before even asking for the
  // three small tiles doubled the perceived load time.
  const networthStart = loadNetworth() // not awaited — the tiles paint whenever it lands
  await _load()
  await networthStart
}

// ---------------------------------------------------------------------------
// Net worth at a glance (user request): three small tiles above the primary
// actions; tapping one opens the detailed Assets & Liabilities page.
// ---------------------------------------------------------------------------
const networth = ref(null)

async function loadNetworth() {
  try {
    networth.value = await api.get('/networth/')
  } catch {
    // Non-fatal — the tiles simply stay hidden.
  }
}

const networthTiles = computed(() => {
  const n = networth.value
  if (!n) return []
  return [
    {
      key: 'assets',
      label: 'Assets',
      value: money(n.assets?.total),
      tone: 'text-success',
    },
    {
      key: 'liabilities',
      label: 'Liabilities',
      value: money(n.liabilities?.total),
      tone: 'text-error',
    },
    {
      key: 'net',
      label: 'Net',
      value: money(n.net),
      tone: Number(n.net) < 0 ? 'text-error' : 'text-primary',
    },
  ]
})

</script>

<template>
  <div>
    <!-- Net worth at a glance (user request): three small tiles above the
         Add Delivery row — tap any of them for the detailed breakup. -->
    <div v-if="networthTiles.length" class="d-flex flex-wrap ga-3 mb-4">
      <v-card
        v-for="t in networthTiles"
        :key="t.key"
        variant="tonal"
        class="flex-grow-1"
        style="min-width: 100px; cursor: pointer"
        @click="router.push('/networth')"
      >
        <v-card-text class="text-center py-2 px-3">
          <div class="text-caption font-weight-bold text-uppercase text-medium-emphasis">
            {{ t.label }}
          </div>
          <div class="text-subtitle-1 font-weight-bold" :class="t.tone">
            {{ t.value }}
          </div>
        </v-card-text>
      </v-card>
    </div>

    <!-- Primary actions (spec 4.1). One size for both: the hierarchy comes from
         colour (flat vs tonal), not from a bigger button — they used to render
         as two different heights and wrapped awkwardly on phones. -->
    <div class="d-flex flex-wrap ga-3 mb-4">
      <v-btn
        color="primary"
        size="large"
        variant="flat"
        prepend-icon="mdi-plus"
        class="flex-grow-1"
        style="min-width: 190px"
        @click="router.push('/delivery/new')"
      >
        Add Delivery
      </v-btn>
      <v-btn
        color="secondary"
        size="large"
        variant="tonal"
        prepend-icon="mdi-truck-plus"
        class="flex-grow-1"
        style="min-width: 190px"
        @click="router.push('/stock/arrived')"
      >
        Enter Arrived Stock
      </v-btn>
    </div>

    <v-alert v-if="error" type="error" class="mb-4" density="compact">
      {{ error }} — check the API connection.
    </v-alert>

    <!-- allow-range: the Home dashboard can also be driven by an explicit
         from/to range instead of the month (user request). -->
    <MonthPicker allow-range />

    <!-- Assets (user request): click the header to expand / collapse, exactly
         like Stock in Hand. The four tiles drive the bar chart below the same
         way the Summary tiles do. -->
    <v-card class="mb-4" :loading="loading">
      <v-card-title
        class="d-flex align-center flex-wrap text-subtitle-1 font-weight-bold"
        style="cursor: pointer"
        @click="assetsOpen = !assetsOpen"
      >
        <v-icon start color="primary">mdi-cash-multiple</v-icon>
        <span class="mr-2">Assets</span>
        <v-spacer />
        <span class="text-caption text-medium-emphasis mr-2">{{ periodLabel }}</span>
        <v-icon>{{ assetsOpen ? 'mdi-chevron-up' : 'mdi-chevron-down' }}</v-icon>
      </v-card-title>
      <v-divider />
      <v-expand-transition>
        <div v-show="assetsOpen">
          <v-card-text>
            <v-row>
              <v-col v-for="t in assetTiles" :key="t.key" cols="6" md="3">
                <v-card
                  :color="t.color"
                  :variant="assetMetric === t.key ? 'flat' : 'tonal'"
                  height="100%"
                  style="cursor: pointer"
                  @click="assetMetric = t.key"
                >
                  <v-card-text class="d-flex flex-column text-center h-100">
                    <div class="text-h5 font-weight-bold">{{ t.value }}</div>
                    <div class="text-caption font-weight-bold text-uppercase">
                      {{ t.label }}
                    </div>
                    <div v-if="t.note" class="text-caption" style="opacity: 0.85">
                      {{ t.note }}
                    </div>
                    <div class="text-caption mt-auto" style="opacity: 0.8">
                      {{ t.caption }}
                    </div>
                  </v-card-text>
                </v-card>
              </v-col>
            </v-row>

            <!-- Chart area: populated by the tile selected above. -->
            <div
              class="text-caption font-weight-bold text-uppercase text-medium-emphasis mt-2"
            >
              {{ assetChart.title }}
            </div>
            <div class="text-caption text-medium-emphasis">
              {{ assetChart.subtitle }}
            </div>
            <ChartCanvas
              v-if="assetChart.labels.length"
              type="bar"
              :labels="assetChart.labels"
              :datasets="[{ label: assetChart.label, data: assetChart.data }]"
              :height="220"
            />
            <EmptyState v-else :text="assetChart.empty" icon="mdi-chart-bar" />
          </v-card-text>
        </div>
      </v-expand-transition>
    </v-card>

    <!-- Stock in Hand: the low / healthy summary always shows; clicking the
         header expands the per-material detail (user request). -->
    <v-card class="mb-4" :loading="loading">
      <v-card-title
        class="d-flex align-center flex-wrap text-subtitle-1 font-weight-bold"
        style="cursor: pointer"
        @click="stockOpen = !stockOpen"
      >
        <v-icon start color="primary">mdi-package-variant-closed</v-icon>
        <span class="mr-2">Stock in Hand</span>
        <v-spacer />
        <!-- Counts only, as coloured badges — no "low" / "above alert" wording
             and no commentary (user request). Order is fixed: low, negative,
             above alert, so each number stays in place. The title wraps on a
             phone so the badges never overlap the label (user request). -->
        <v-chip
          size="small"
          variant="flat"
          class="ml-2 mr-1 font-weight-bold"
          :color="belowAlert.length ? 'error' : 'success'"
        >
          {{ belowAlert.length }}
        </v-chip>
        <v-chip
          size="small"
          variant="flat"
          class="mr-1 font-weight-bold"
          :color="negativeStock.length ? 'error' : 'success'"
        >
          {{ negativeStock.length }}
        </v-chip>
        <v-chip
          size="small"
          variant="flat"
          class="mr-1 font-weight-bold"
          :color="aboveAlert.length ? 'success' : 'error'"
        >
          {{ aboveAlert.length }}
        </v-chip>
        <v-icon>{{ stockOpen ? 'mdi-chevron-up' : 'mdi-chevron-down' }}</v-icon>
      </v-card-title>
      <v-divider />
      <v-expand-transition>
        <div v-show="stockOpen">
          <EmptyState
            v-if="!loading && !stock.length"
            icon="mdi-package-variant"
            text="No materials yet — add them in Masters → MVP Master."
            action="Open MVP Master"
            @action="router.push('/masters/materials')"
          />
          <div v-else class="stock-tiles">
            <div
              v-for="s in stock"
              :key="s.id"
              class="stock-tile"
              :class="s.below_alert || Number(s.available) < 0 ? 'is-low' : 'is-ok'"
              role="link"
              tabindex="0"
              @click="router.push(`/masters/materials/${s.id}`)"
              @keyup.enter="router.push(`/masters/materials/${s.id}`)"
            >
              <div class="stock-tile__name" :title="s.name">{{ s.name }}</div>
              <div class="stock-tile__qty">
                {{ num(s.available) }}
                <span v-if="s.unit" class="stock-tile__unit">{{ s.unit }}</span>
              </div>
              <div v-if="s.client_name" class="stock-tile__meta">
                {{ s.client_name }}
              </div>
              <div v-if="Number(s.committed) > 0" class="stock-tile__committed">
                {{ num(s.committed) }} on orders
              </div>
              <div
                v-if="s.has_unrecorded_shortfall"
                class="stock-tile__committed"
                :title="`Delivered ${num(s.demand)} but only ${num(s.booked)} booked`"
              >
                {{ num(s.unrecorded_shortfall) }} unbooked
              </div>
            </div>
          </div>
        </div>
      </v-expand-transition>
    </v-card>

    <!-- Delivery register export (user request): pick a range, preview the
         rows the server returns for it, then download the PDF with revenue
         and profit totals. -->
    <ExportDialog
      v-model="exportOpen"
      title="Export delivery register"
      range-label="Delivery register — date, client, SKU, quantity, revenue and profit"
      date-label="Register range"
      :loader="loadExportRows"
      :default-from="exportDefaults.from"
      :default-to="exportDefaults.to"
      :columns="registerColumns"
      empty-hint="No deliveries in this date range."
      confirm-label="Export PDF"
      @confirm="runExport"
    />

    <!-- Summary — MMM, YYYY: the tiles drive the chart area below (user request). -->
    <v-card>
      <v-card-title class="d-flex align-center text-subtitle-1 font-weight-bold">
        <v-icon start color="primary">mdi-chart-box</v-icon>
        Summary — {{ periodLabel }}
      </v-card-title>
      <v-divider />
      <v-card-text>
        <v-row>
          <v-col v-for="t in tiles" :key="t.key" cols="6" md="3">
            <v-card
              :color="t.color"
              :variant="metric === t.key ? 'flat' : 'tonal'"
              :style="t.selectable ? 'cursor: pointer' : ''"
              height="100%"
              @click="t.selectable ? (metric = t.key) : null"
            >
              <v-card-text class="d-flex flex-column text-center h-100">
                <div class="text-h5 font-weight-bold">{{ t.value }}</div>
                <div class="text-caption font-weight-bold text-uppercase">
                  {{ t.label }}
                </div>
                <div v-if="t.note" class="text-caption" style="opacity: 0.85">
                  {{ t.note }}
                </div>
                <div class="text-caption mt-auto" style="opacity: 0.8">
                  {{ t.caption }}
                </div>
              </v-card-text>
            </v-card>
          </v-col>
        </v-row>

        <!-- Chart area: populated by the tile selected above (user request). -->
        <div class="d-flex flex-wrap align-center mt-4">
          <div>
            <!-- Section label: same 12px uppercase treatment as every other
                 in-card label, so nothing is "big" or "small" by accident. -->
            <div
              class="text-caption font-weight-bold text-uppercase text-medium-emphasis"
            >
              {{ chart.title }}
            </div>
            <div class="text-caption text-medium-emphasis">
              {{ chart.subtitle }}
            </div>
          </div>
          <v-spacer />
          <v-switch
            v-if="metric === 'profit'"
            v-model="includeOverhead"
            color="warning"
            density="compact"
            hide-details
            label="Include overhead"
            class="flex-grow-0"
          />
        </div>
        <ChartCanvas
          v-if="chart.labels.length"
          type="bar"
          :labels="chart.labels"
          :datasets="[{ label: chart.label, data: chart.data }]"
          :height="240"
          @select="onMainSelect"
        />
        <EmptyState v-else :text="chart.empty" icon="mdi-chart-bar" />
        <div
          v-if="chart.labels.length"
          class="text-caption text-medium-emphasis mt-1"
        >
          Tap a bar to open its break-up below.
        </div>

        <!-- Chart drill-downs (user request): tap a bar above for the rows
             underneath — cases -> clients, revenue -> SKUs, profit -> SKUs
             -> the full cost chain. -->
        <div v-if="metric === 'cases' && casesDrill" class="mt-3">
          <div
            class="text-caption font-weight-bold text-uppercase text-medium-emphasis"
          >
            {{ casesDrill.title }}
          </div>
          <div class="text-caption text-medium-emphasis">
            {{ casesDrill.subtitle }}
          </div>
          <ChartCanvas
            type="bar"
            :labels="casesDrill.labels"
            :datasets="[{ label: casesDrill.label, data: casesDrill.data }]"
            :height="200"
          />
        </div>

        <div v-else-if="metric === 'revenue' && revenueDrill" class="mt-3">
          <div
            class="text-caption font-weight-bold text-uppercase text-medium-emphasis"
          >
            {{ revenueDrill.title }}
          </div>
          <div class="text-caption text-medium-emphasis">
            {{ revenueDrill.subtitle }}
          </div>
          <ChartCanvas
            type="bar"
            :labels="revenueDrill.labels"
            :datasets="[
              { label: revenueDrill.label, data: revenueDrill.data },
            ]"
            :height="200"
          />
          <div class="mt-2">
            <div
              v-for="r in revenueDrill.rows"
              :key="r.sku_id"
              class="d-flex align-center py-1"
            >
              <span class="text-body-2">{{ r.sku }}</span>
              <span class="text-caption text-medium-emphasis ml-2">
                {{ num(r.cases, 0) }} cases
              </span>
              <v-spacer />
              <span class="text-body-2 font-weight-medium">
                {{ money(r.revenue) }}
              </span>
            </div>
            <div class="d-flex align-center py-1">
              <span class="text-caption font-weight-bold text-uppercase">
                Total revenue
              </span>
              <v-spacer />
              <span class="text-body-2 font-weight-bold">
                {{ money(revenueDrill.totalRevenue) }}
              </span>
            </div>
            <div class="text-caption text-medium-emphasis">
              Matches the revenue bar above — same client, same month, same
              deliveries.
            </div>
          </div>
        </div>

        <div v-else-if="metric === 'profit' && profitDrill" class="mt-3">
          <div
            class="text-caption font-weight-bold text-uppercase text-medium-emphasis"
          >
            {{ profitDrill.title }}
          </div>
          <div class="text-caption text-medium-emphasis">
            {{ profitDrill.subtitle }}
          </div>
          <ChartCanvas
            type="bar"
            :labels="profitDrill.labels"
            :datasets="[{ label: profitDrill.label, data: profitDrill.data }]"
            :height="200"
            @select="onProfitDrillSelect"
          />
          <div v-if="profitSkuDetail" class="mt-3">
            <div
              class="text-caption font-weight-bold text-uppercase text-medium-emphasis"
            >
              Cost breakup — {{ profitSkuDetail.row.sku }} ·
              {{ num(profitSkuDetail.row.cases, 0) }} cases
            </div>
            <v-table density="compact" class="mt-1">
              <tbody>
                <tr>
                  <td>Revenue</td>
                  <td class="text-right">
                    {{ money(profitSkuDetail.row.revenue) }}
                  </td>
                </tr>
                <tr>
                  <td>Raw materials (current FIFO)</td>
                  <td class="text-right">
                    {{ money(profitSkuDetail.row.materials_cost) }}
                  </td>
                </tr>
                <tr>
                  <td>Print / label (current)</td>
                  <td class="text-right">
                    {{ money(profitSkuDetail.row.print_cost) }}
                  </td>
                </tr>
                <tr v-for="c in profitSkuDetail.categories" :key="c.category">
                  <td class="pl-8">
                    Overhead — {{ c.category }}
                    <span class="text-caption text-medium-emphasis">
                      ({{ money(c.month) }} for the month)
                    </span>
                  </td>
                  <td class="text-right">{{ money(c.share) }}</td>
                </tr>
                <tr v-if="!profitSkuDetail.categories.length">
                  <td>Overhead share</td>
                  <td class="text-right">
                    {{ money(profitSkuDetail.row.overhead) }}
                  </td>
                </tr>
                <tr>
                  <td>Profit before overhead</td>
                  <td class="text-right font-weight-medium">
                    {{ money(profitSkuDetail.row.profit_excl_overhead) }}
                  </td>
                </tr>
                <tr>
                  <td class="font-weight-bold">Profit after overhead</td>
                  <td class="text-right font-weight-bold">
                    {{ money(profitSkuDetail.row.profit_incl_overhead) }}
                  </td>
                </tr>
              </tbody>
            </v-table>
            <div class="text-caption text-medium-emphasis mt-1">
              Overhead = {{ num(profitSkuDetail.row.cases, 0) }} cases ×
              {{ money(stats.overhead_per_case) }} / case in
              {{ periodLabel }} — the chain adds up to the profit bar
              above.
            </div>
          </div>
        </div>

        <!-- Deliveries and Inward material are their own collapsible panels
             below, so the Summary card holds only the tiles and the chart. -->
      </v-card-text>
    </v-card>

    <!-- Deliveries — chronological register for the month (user request).
         Collapses the same way as Stock in Hand. -->
    <v-card class="mt-4" :loading="loading">
      <v-card-title
        class="d-flex align-center text-subtitle-1 font-weight-bold"
        style="cursor: pointer"
        @click="deliveriesOpen = !deliveriesOpen"
      >
        <v-icon start color="primary">mdi-truck-delivery-outline</v-icon>
        Deliveries
        <v-spacer />
        <span class="text-caption text-medium-emphasis mr-1">
          {{ deliveryTotals.count }} · {{ num(deliveryTotals.cases) }}
        </span>
        <v-btn
          size="x-small"
          variant="tonal"
          color="primary"
          prepend-icon="mdi-file-pdf-box"
          class="mr-2"
          title="Export this register as a PDF for any date range"
          @click.stop="exportOpen = true"
        >
          Export PDF
        </v-btn>
        <v-icon>{{ deliveriesOpen ? 'mdi-chevron-up' : 'mdi-chevron-down' }}</v-icon>
      </v-card-title>
      <v-divider />
      <v-expand-transition>
        <div v-show="deliveriesOpen">
          <v-card-text>
            <v-table density="compact">
              <thead>
                <tr>
                  <th class="text-left">Date</th>
                  <th class="text-left">Client</th>
                  <th class="text-left">SKU</th>
                  <th class="text-right">Qty</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="d in deliveryRows"
                  :key="d.id"
                  class="clickable-row"
                  @click="router.push(`/clients/${d.client_id}`)"
                >
                  <td class="text-no-wrap">{{ d.date }}</td>
                  <td>{{ d.client }}</td>
                  <td class="text-no-wrap">{{ d.sku }}</td>
                  <td class="text-right font-weight-medium">
                    {{ num(d.qty_cases) }}
                  </td>
                </tr>
              </tbody>
            </v-table>
            <p
              v-if="!deliveryRows.length && !loading"
              class="text-body-2 text-medium-emphasis mb-0"
            >
              No deliveries this month.
            </p>
            <div
              v-if="deliveryRows.length"
              class="text-right text-body-2 font-weight-bold mt-2"
            >
              {{ num(deliveryTotals.cases) }}
            </div>
          </v-card-text>
        </div>
      </v-expand-transition>
    </v-card>

    <!-- Inward material — chronological arrivals for the month (user request):
         a flat register table with material filter chips and the month
         reconciliation. Line items stay editable in place. -->
    <v-card class="mt-4" :loading="loading">
      <v-card-title
        class="d-flex align-center text-subtitle-1 font-weight-bold"
        style="cursor: pointer"
        @click="inwardPanelOpen = !inwardPanelOpen"
      >
        <v-icon start color="primary">mdi-truck-arrow-right</v-icon>
        Inward Material
        <v-spacer />
        <span class="text-caption text-medium-emphasis mr-1">
          {{ num(inwardTotal) }}
        </span>
        <v-icon>{{ inwardPanelOpen ? 'mdi-chevron-up' : 'mdi-chevron-down' }}</v-icon>
      </v-card-title>
      <v-divider />
      <v-expand-transition>
        <div v-show="inwardPanelOpen">
          <v-card-text>
            <!-- Month reconciliation: carry forward + inward - delivered = in hand. -->
            <v-row class="mb-1">
              <v-col v-for="s in inwardSummaryTiles" :key="s.label" cols="6" md="3">
                <div class="text-body-2">
                  <div class="text-caption text-medium-emphasis text-uppercase">
                    {{ s.label }}
                  </div>
                  <div class="text-h6 font-weight-bold" :class="s.tone">
                    {{ s.value }}
                  </div>
                </div>
              </v-col>
            </v-row>

            <!-- Material filter: "All" plus one chip per material that arrived. -->
            <div class="d-flex flex-wrap ga-1 my-3">
              <v-chip
                size="small"
                variant="flat"
                :color="inwardFilter === null ? 'primary' : undefined"
                class="font-weight-medium"
                @click="inwardFilter = null"
              >
                All
              </v-chip>
              <v-chip
                v-for="m in inwardMaterials"
                :key="m.id"
                size="small"
                variant="flat"
                :color="inwardFilter === m.id ? 'primary' : undefined"
                class="font-weight-medium"
                @click="inwardFilter = m.id"
              >
                {{ m.name }}
              </v-chip>
            </div>

            <v-table v-if="visibleInwardRows.length" density="compact">
              <thead>
                <tr>
                  <th class="text-left">Date</th>
                  <th class="text-left">Material</th>
                  <th class="text-right">Received</th>
                  <th class="text-right">Consumed</th>
                  <th class="text-right">Available</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in visibleInwardRows" :key="item.id">
                  <template v-if="inwardDraft[item.id]">
                    <td>
                      <v-text-field
                        v-model="inwardDraft[item.id].arrival_date"
                        type="date"
                        density="compact"
                        hide-details
                        label="Date"
                      />
                    </td>
                    <td class="text-body-2">{{ item.material }}</td>
                    <td>
                      <v-text-field
                        v-model.number="inwardDraft[item.id].quantity_received"
                        type="number"
                        step="any"
                        min="0"
                        density="compact"
                        hide-details
                        label="Received (0 = cancel)"
                      />
                    </td>
                    <td class="text-right text-body-2 text-medium-emphasis">
                      {{ num(item.batch_consumed) }}
                    </td>
                    <td class="text-right text-body-2 text-medium-emphasis">
                      {{ num(item.available) }}
                    </td>
                    <td class="text-no-wrap">
                      <v-btn
                        icon="mdi-content-save"
                        size="x-small"
                        variant="text"
                        color="primary"
                        :loading="inwardSaving === item.id"
                        @click="saveItem(item)"
                      />
                      <v-btn
                        icon="mdi-close"
                        size="x-small"
                        variant="text"
                        @click="cancelEdit(item.id)"
                      />
                    </td>
                  </template>
                  <template v-else>
                    <td class="text-no-wrap">{{ item.date }}</td>
                    <td>{{ item.material }}</td>
                    <td class="text-right font-weight-medium">
                      {{ num(item.quantity_received) }}
                    </td>
                    <td class="text-right text-body-2">
                      {{ num(item.batch_consumed) }}
                    </td>
                    <td class="text-right text-body-2">
                      {{ num(item.available) }}
                    </td>
                    <td class="text-no-wrap">
                      <v-btn
                        icon="mdi-pencil-outline"
                        size="x-small"
                        variant="text"
                        @click="startEdit(item)"
                      />
                      <v-btn
                        icon="mdi-delete-outline"
                        size="x-small"
                        variant="text"
                        color="error"
                        @click="deleteItem(item)"
                      />
                    </td>
                  </template>
                </tr>
              </tbody>
              <tfoot>
                <tr class="text-body-2">
                  <td colspan="2" class="font-weight-bold">Total</td>
                  <td class="text-right font-weight-bold">
                    {{ num(inwardLineTotals.received) }}
                  </td>
                  <td class="text-right font-weight-bold">
                    {{ num(inwardLineTotals.consumed) }}
                  </td>
                  <td class="text-right font-weight-bold">
                    {{ num(inwardLineTotals.available) }}
                  </td>
                  <td></td>
                </tr>
              </tfoot>
            </v-table>
            <p v-else-if="!loading" class="text-body-2 text-medium-emphasis mb-0">
              No stock arrivals this month.
            </p>
          </v-card-text>
        </div>
      </v-expand-transition>
    </v-card>
  </div>
</template>

<style scoped>
/* Register rows open the client / batch they point at. */
.clickable-row {
  cursor: pointer;
}

.clickable-row:hover {
  background: rgba(var(--v-theme-primary), 0.06);
}

/* Stock in Hand tiles (user request): small cards, one per material, tinted by
   state. The colour is the only signal — red when the material is below its
   alert level or has gone negative, green otherwise — so a glance at the grid
   replaces reading the rows it replaced. */
.stock-tiles {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.stock-tile {
  flex: 1 1 130px;
  max-width: 200px;
  min-width: 120px;
  padding: 10px 12px;
  border-radius: 10px;
  cursor: pointer;
  /* Colour, not just text: the tile itself carries the state. */
  border: 1px solid transparent;
  transition: transform 120ms ease, box-shadow 120ms ease;
}

.stock-tile:hover {
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
}

.stock-tile.is-ok {
  background: rgba(var(--v-theme-success), 0.16);
  border-color: rgba(var(--v-theme-success), 0.45);
}

.stock-tile.is-low {
  background: rgba(var(--v-theme-error), 0.16);
  border-color: rgba(var(--v-theme-error), 0.5);
}

.stock-tile__name {
  font-size: 0.8125rem;
  font-weight: 600;
  line-height: 1.25;
  /* Two lines max, then ellipsis — long material names must not blow up the
     grid's row height. */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.stock-tile__qty {
  font-size: 1.375rem;
  font-weight: 700;
  line-height: 1.2;
  margin-top: 2px;
}

.stock-tile__unit {
  font-size: 0.75rem;
  font-weight: 500;
  opacity: 0.7;
}

.stock-tile__meta,
.stock-tile__committed {
  font-size: 0.6875rem;
  opacity: 0.75;
  line-height: 1.3;
}

/* Mobile: the register tables must scroll sideways rather than squash the
   columns into each other (user request — no overlapping text). */
@media (max-width: 600px) {
  :deep(table) {
    font-size: 0.8125rem;
  }

  :deep(td),
  :deep(th) {
    white-space: nowrap;
    padding-inline: 6px;
  }
}
</style>

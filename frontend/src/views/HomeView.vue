<script setup>
/**
 * Home (spec 4.1, reworked per user request): FAB for + Add Delivery, secondary
 * + Enter Arrived Stock, a Stock in Hand panel that always shows the low /
 * healthy summary and expands on click, and a "Summary — MMM, YYYY" card whose
 * tiles (cases sold / revenue / profit / overhead) drive ONE chart area.
 * The month's inward material arrivals sit at the bottom with editable line
 * items — correcting one recalculates stock, FIFO cost and the payable.
 */
import { ref, watch, onMounted, onActivated, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, num, monthLong, rangeLong } from '@/utils/format'
import MonthPicker from '@/components/MonthPicker.vue'
import ChartCanvas from '@/components/ChartCanvas.vue'
import EmptyState from '@/components/EmptyState.vue'
import OrderDialog from '@/components/OrderDialog.vue'

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
        stock_in_hand: group.stock_in_hand,
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
const inwardSummary = computed(() => {
  const rows = visibleInwardRows.value
  const inward = rows.reduce((s, r) => s + Number(r.quantity_received), 0)
  const delivered = rows.reduce((s, r) => s + Number(r.month_consumed), 0)
  const inHand = rows.reduce((s, r) => s + Number(r.stock_in_hand), 0)
  return { inward, delivered, inHand, carryForward: inHand + delivered - inward }
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
  inwardSaving.value = item.id
  error.value = ''
  try {
    await api.patch(`/batches/${item.id}/`, {
      quantity_received: String(draft.quantity_received),
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

async function deleteItem(item) {
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
    error.value = e.message
  } finally {
    inwardSaving.value = null
  }
}

// ---------------------------------------------------------------------------
// Orders pipeline (user request)
// ---------------------------------------------------------------------------
// One endpoint feeds both panels: /orders/summary/ returns the pending orders,
// the Ready to Deliver bucket and the per-material commitment behind it, so the
// two collapsible panels never disagree about the same stock.
const pendingOrders = ref([])
const readyOrders = ref([])
const commitment = ref([])
const pendingExpanded = ref(false)
const readyExpanded = ref(false)
const orderDialog = ref(false)
const editingOrder = ref(null) // null = new order, otherwise the order to edit

const pendingCount = computed(() => pendingOrders.value.length)
const readyCount = computed(() => readyOrders.value.length)

async function loadOrders() {
  try {
    const data = await api.get('/orders/summary/')
    pendingOrders.value = data.pending || []
    readyOrders.value = data.ready || []
    commitment.value = data.commitment || []
  } catch (e) {
    error.value = e.message
  }
}

// The order panels refresh with the dashboard: commitment, the stock tiles and
// the two panels then always describe the same state of the warehouse.
const _load = load
async function loadAll() {
  await _load()
  await loadOrders()
}

function openOrder(order) {
  editingOrder.value = order
  orderDialog.value = true
}

function onOrderSaved() {
  // Commitment changed, so the dashboard's stock tiles (which show available)
  // and both order panels have to be re-read.
  loadAll()
}

function dateShort(iso) {
  if (!iso) return ''
  const d = new Date(`${iso}T00:00:00`)
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

onMounted(loadAll)
onActivated(loadAll)

// Enter Order / edit order modal. One dialog for both, as the lines are
// captured identically — editing simply starts from the saved order.
const closeOrderDialog = () => {
  orderDialog.value = false
  editingOrder.value = null
}

const onOrderClosed = () => {
  closeOrderDialog()
  onOrderSaved()
}
</script>

<template>
  <div>
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
      <v-btn
        color="info"
        size="large"
        variant="tonal"
        prepend-icon="mdi-clipboard-text-outline"
        class="flex-grow-1"
        style="min-width: 190px"
        @click="orderDialog = true"
      >
        Enter Order
      </v-btn>
    </div>

    <v-alert v-if="error" type="error" class="mb-4" density="compact">
      {{ error }} — check the API connection.
    </v-alert>

    <!-- allow-range: the Home dashboard can also be driven by an explicit
         from/to range instead of the month (user request). -->
    <MonthPicker allow-range />

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

    <!-- Pending orders: orders logged at Enter Order that have not yet been
         moved to Ready to Deliver. Collapsible like Stock in Hand, with a
         yellow count badge in the header (user request). Tapping a row opens
         the order for editing. -->
    <v-card v-if="pendingCount" class="mb-4">
      <v-card-item class="py-3" @click="pendingExpanded = !pendingExpanded">
        <div class="d-flex align-center ga-3">
          <v-chip color="warning" variant="flat" size="small" class="font-weight-bold">
            {{ pendingCount }}
          </v-chip>
          <span class="text-uppercase font-weight-bold text-medium-emphasis">
            Pending Orders
          </span>
          <v-spacer />
          <v-btn
            variant="text"
            size="small"
            color="primary"
            @click.stop="router.push('/statusupdate')"
          >
            Open status update
          </v-btn>
          <v-icon>{{ pendingExpanded ? "mdi-chevron-up" : "mdi-chevron-down" }}</v-icon>
        </div>
      </v-card-item>
      <v-expand-transition>
        <div v-show="pendingExpanded">
          <v-card-text class="pt-0">
            <v-list density="compact">
              <v-list-item
                v-for="o in pendingOrders"
                :key="o.id"
                :title="`#${o.id} — ${o.client_name}`"
                :subtitle="`${num(o.total_qty)} cases · ${dateShort(o.order_date)}`"
                @click="openOrder(o)"
              >
                <template #append>
                  <v-icon size="small">mdi-pencil-outline</v-icon>
                </template>
              </v-list-item>
            </v-list>
          </v-card-text>
        </div>
      </v-expand-transition>
    </v-card>

    <!-- Ready to Deliver: orders whose material has left Stock in Hand. Same
         collapsible pattern as the two panels above. The commitment breakdown
         under it says which materials are spoken for and whether the promise
         can still be met from what is free. -->
    <v-card v-if="readyCount" class="mb-4">
      <v-card-item class="py-3" @click="readyExpanded = !readyExpanded">
        <div class="d-flex align-center ga-3">
          <v-chip color="success" variant="flat" size="small" class="font-weight-bold">
            {{ readyCount }}
          </v-chip>
          <span class="text-uppercase font-weight-bold text-medium-emphasis">
            Ready to Deliver
          </span>
          <v-spacer />
          <v-btn
            variant="text"
            size="small"
            color="primary"
            @click.stop="router.push('/delivery/new?from=ready')"
          >
            Deliver these
          </v-btn>
          <v-icon>{{ readyExpanded ? "mdi-chevron-up" : "mdi-chevron-down" }}</v-icon>
        </div>
      </v-card-item>
      <v-expand-transition>
        <div v-show="readyExpanded">
          <v-card-text class="pt-0">
            <v-alert
              v-if="commitment.some((c) => c.short)"
              type="warning"
              variant="tonal"
              density="compact"
              class="mb-3"
            >
              Some committed material is no longer free — see the breakdown below.
            </v-alert>
            <v-list density="compact">
              <v-list-item
                v-for="o in readyOrders"
                :key="o.id"
                :title="`#${o.id} — ${o.client_name}`"
                :subtitle="`${num(o.total_qty)} cases · ${dateShort(o.order_date)}`"
                @click="openOrder(o)"
              >
                <template #append>
                  <v-icon size="small">mdi-pencil-outline</v-icon>
                </template>
              </v-list-item>
            </v-list>
            <div v-if="commitment.length" class="text-uppercase text-medium-emphasis mt-3 mb-1">
              Material committed
            </div>
            <v-table v-if="commitment.length" density="compact">
              <thead>
                <tr>
                  <th>Material</th>
                  <th class="text-right">Committed</th>
                  <th class="text-right">Available</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="c in commitment" :key="c.material_id">
                  <td>{{ c.material }}</td>
                  <td class="text-right">{{ num(c.committed) }} {{ c.unit }}</td>
                  <td
                    class="text-right font-weight-bold"
                    :class="c.short ? 'text-error' : 'text-success'"
                  >
                    {{ num(c.available) }}
                  </td>
                </tr>
              </tbody>
            </v-table>
          </v-card-text>
        </div>
      </v-expand-transition>
    </v-card>

    <!-- Enter Order / edit order. Both use the same dialog — editing simply
         starts from the saved order's lines. -->
    <OrderDialog
      v-model="orderDialog"
      :order="editingOrder"
      @closed="onOrderClosed"
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
                  <th class="text-right">Qty</th>
                  <th class="text-right">In hand</th>
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
                        density="compact"
                        hide-details
                        label="Qty"
                      />
                    </td>
                    <td class="text-right text-body-2 text-medium-emphasis">
                      {{ num(item.stock_in_hand) }}
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
                      {{ num(item.stock_in_hand) }}
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

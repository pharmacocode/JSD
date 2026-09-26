<script setup>
/**
 * Home (spec 4.1, reworked per user request): FAB for + Add Delivery, secondary
 * + Enter Arrived Stock, a Stock in Hand panel that always shows the low /
 * healthy summary and expands on click, and a "Summary — MMM, YYYY" card whose
 * tiles (cases sold / revenue / profit / overhead) drive ONE chart area.
 * The month's inward material arrivals sit at the bottom with editable line
 * items — correcting one recalculates stock, FIFO cost and the payable.
 */
import { ref, watch, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, num, monthLong } from '@/utils/format'
import MonthPicker from '@/components/MonthPicker.vue'
import ChartCanvas from '@/components/ChartCanvas.vue'
import EmptyState from '@/components/EmptyState.vue'

const router = useRouter()
const ui = useUiStore()
const loading = ref(true)
const data = ref({ stock: [], stats: {} })
const error = ref('')

// Panel / chart state.
const stockOpen = ref(false)
const metric = ref('cases') // cases | revenue | profit — drives the chart area
const includeOverhead = ref(true) // profit toggle (user request)
const inwardOpen = ref([]) // material ids expanded in the inward list
const inwardDraft = ref({}) // batch id -> editable working copy
const inwardSaving = ref(null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.get('/dashboard/', { month: ui.month })
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => ui.month, load)

const stats = computed(() => data.value.stats || {})
const stock = computed(() => data.value.stock || [])
const belowAlert = computed(() => stock.value.filter((s) => s.below_alert))
const aboveAlert = computed(() => stock.value.filter((s) => !s.below_alert))
// Negative stock = delivered beyond what was in hand (user request): counted
// separately so the user Stock-Adjusts or backdates an arrival to clear it.
const negativeStock = computed(() =>
  stock.value.filter((s) => Number(s.stock_in_hand) < 0)
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
watch([metric, () => ui.month], () => {
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
    subtitle: `${monthLong(ui.month)} · ${num(totalCases, 0)} cases sold`,
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
    subtitle: `${monthLong(ui.month)} · case counts, revenue adds up to the bar`,
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
    subtitle: `${monthLong(ui.month)} · ${
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
function toggleInward(id) {
  const i = inwardOpen.value.indexOf(id)
  if (i === -1) inwardOpen.value.push(id)
  else inwardOpen.value.splice(i, 1)
}

function isOpen(id) {
  return inwardOpen.value.includes(id)
}

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
    </div>

    <v-alert v-if="error" type="error" class="mb-4" density="compact">
      {{ error }} — check the API connection.
    </v-alert>

    <MonthPicker />

    <!-- Stock in Hand: the low / healthy summary always shows; clicking the
         header expands the per-material detail (user request). -->
    <v-card class="mb-4" :loading="loading">
      <v-card-title
        class="d-flex align-center text-subtitle-1 font-weight-bold"
        style="cursor: pointer"
        @click="stockOpen = !stockOpen"
      >
        <v-icon start color="primary">mdi-package-variant-closed</v-icon>
        Stock in Hand
        <v-chip
          v-if="belowAlert.length"
          color="error"
          size="small"
          variant="flat"
          class="ml-2"
        >
          {{ belowAlert.length }} low
        </v-chip>
        <v-chip
          v-if="negativeStock.length"
          color="error"
          size="small"
          variant="tonal"
          class="ml-2"
          prepend-icon="mdi-alert"
        >
          {{ negativeStock.length }} negative
        </v-chip>
        <v-chip
          v-if="aboveAlert.length"
          color="success"
          size="small"
          variant="flat"
          class="ml-2"
        >
          {{ aboveAlert.length }} above alert
        </v-chip>
        <v-spacer />
        <span class="text-caption text-medium-emphasis mr-2">
          {{ stock.length }} material{{ stock.length === 1 ? '' : 's' }}
        </span>
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
          <v-list v-else density="compact">
            <v-list-item
              v-for="s in stock"
              :key="s.id"
              :to="`/masters/materials/${s.id}`"
              :title="s.name"
              :subtitle="`${s.client_name ? 'Client: ' + s.client_name : s.category} · alert at ${num(
                s.stock_alert_qty
              )} ${s.unit}`"
            >
              <template #append>
                <v-chip
                  :color="s.below_alert ? 'error' : 'success'"
                  size="small"
                  variant="flat"
                  class="mr-1"
                >
                  {{ num(s.stock_in_hand) }} {{ s.unit }}
                </v-chip>
                <v-chip
                  v-if="Number(s.stock_in_hand) < 0"
                  color="error"
                  size="x-small"
                  variant="flat"
                >
                  SHORT
                </v-chip>
                <v-chip
                  v-else-if="s.below_alert"
                  color="error"
                  size="x-small"
                  variant="tonal"
                >
                  LOW
                </v-chip>
                <v-chip
                  v-if="s.has_unrecorded_shortfall"
                  color="warning"
                  size="x-small"
                  variant="flat"
                  class="ml-1"
                  :title="`Delivered ${num(s.demand)} but only ${num(s.booked)} booked — run the legacy backfill or add the missing arrival`"
                >
                  UNRECORDED −{{ num(s.unrecorded_shortfall) }}
                </v-chip>
              </template>
            </v-list-item>
          </v-list>
          <div class="text-caption text-medium-emphasis pa-3 pt-2">
            Red = below the alert quantity · green = at or above it · SHORT =
            negative stock (delivered beyond stock in hand) — clear it with a
            Stock Adjustment or a backdated arrival. UNRECORDED −n = delivered
            demand the batch ledger never booked (pre-negative-stock entries) —
            run the legacy backfill to book it. Tap a material to open
            its batches.
          </div>
        </div>
      </v-expand-transition>
    </v-card>

    <!-- Summary — MMM, YYYY: the tiles drive the chart area below (user request). -->
    <v-card>
      <v-card-title class="d-flex align-center text-subtitle-1 font-weight-bold">
        <v-icon start color="primary">mdi-chart-box</v-icon>
        Summary — {{ monthLong(ui.month) }}
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
              {{ monthLong(ui.month) }} — the chain adds up to the profit bar
              above.
            </div>
          </div>
        </div>

        <!-- Inward material this month — the line items are editable in place. -->
        <div
          class="text-caption font-weight-bold text-uppercase text-medium-emphasis mt-4 mb-1"
        >
          Inward material ({{ monthLong(ui.month) }})
        </div>
        <div v-if="stats.inward_materials?.length">
          <div v-for="row in stats.inward_materials" :key="row.material_id">
            <div
              class="d-flex align-center py-1"
              style="cursor: pointer"
              @click="toggleInward(row.material_id)"
            >
              <v-icon size="small" class="mr-1">
                {{ isOpen(row.material_id) ? 'mdi-chevron-down' : 'mdi-chevron-right' }}
              </v-icon>
              <span class="text-body-2">{{ row.material }}</span>
              <v-chip
                v-if="row.items.length > 1"
                size="x-small"
                variant="tonal"
                class="ml-2"
              >
                {{ row.items.length }} arrivals
              </v-chip>
              <v-spacer />
              <span class="text-body-2 font-weight-medium">
                {{ num(row.quantity) }} {{ row.unit }}
              </span>
            </div>

            <div v-if="isOpen(row.material_id)" class="ml-6 mb-2">
              <div
                v-for="item in row.items"
                :key="item.id"
                class="d-flex flex-wrap align-center ga-2 py-1"
              >
                <template v-if="inwardDraft[item.id]">
                  <v-text-field
                    v-model.number="inwardDraft[item.id].quantity_received"
                    type="number"
                    step="any"
                    density="compact"
                    hide-details
                    label="Received"
                    style="max-width: 120px"
                  />
                  <v-text-field
                    v-model.number="inwardDraft[item.id].price_per_unit"
                    type="number"
                    step="0.01"
                    prefix="₹"
                    density="compact"
                    hide-details
                    label="Price / case"
                    style="max-width: 150px"
                  />
                  <v-text-field
                    v-model="inwardDraft[item.id].arrival_date"
                    type="date"
                    density="compact"
                    hide-details
                    label="Arrival date"
                    style="max-width: 170px"
                  />
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
                </template>
                <template v-else>
                  <span class="text-caption text-medium-emphasis" style="width: 92px">
                    {{ item.date }}
                  </span>
                  <span class="text-body-2">
                    {{ num(item.quantity_received) }} {{ row.unit }}
                  </span>
                  <span class="text-caption text-medium-emphasis">
                    · in hand {{ num(item.quantity_remaining) }} · consumed
                    {{ num(item.consumed) }}
                  </span>
                  <v-spacer />
                  <span class="text-caption">{{ money(item.value) }}</span>
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
                </template>
              </div>
              <div class="text-caption text-medium-emphasis mt-1">
                Editing a line item recalculates stock in hand, the FIFO cost and
                the vendor's payable. Deliveries already made keep the cost that
                applied when they were entered.
              </div>
            </div>
          </div>
        </div>
        <p v-else class="text-body-2 text-medium-emphasis">
          No stock arrivals this month.
        </p>
      </v-card-text>
    </v-card>
  </div>
</template>


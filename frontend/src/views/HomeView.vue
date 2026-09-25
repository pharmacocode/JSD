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
      labels: rows.map((r) => r.sku),
      data: rows.map((r) => Number(r.cases)),
      label: 'Cases',
      empty: 'No sales this month yet.',
    }
  }
  const key = metric.value === 'profit' ? profitKey.value : 'revenue'
  const rows = [...(stats.value.client_breakdown || [])].sort(
    (a, b) => Number(b[key]) - Number(a[key])
  )
  return {
    title:
      metric.value === 'profit'
        ? `Profit per client — ${includeOverhead.value ? 'after' : 'before'} overhead`
        : 'Revenue per client (highest first)',
    labels: rows.map((r) => r.client),
    data: rows.map((r) => Number(r[key])),
    label: metric.value === 'profit' ? 'Profit (₹)' : 'Revenue (₹)',
    empty: 'No client figures this month.',
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
    <!-- Primary action: big FAB (spec 4.1) -->
    <div class="d-flex flex-wrap ga-3 mb-4">
      <v-btn
        color="primary"
        size="x-large"
        prepend-icon="mdi-plus"
        class="flex-grow-1"
        @click="router.push('/delivery/new')"
      >
        Add Delivery
      </v-btn>
      <v-btn
        color="secondary"
        size="large"
        variant="elevated"
        prepend-icon="mdi-truck-plus"
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
        class="d-flex align-center"
        style="cursor: pointer"
        @click="stockOpen = !stockOpen"
      >
        <v-icon start>mdi-package-variant-closed</v-icon>
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
          v-if="aboveAlert.length"
          color="success"
          size="small"
          variant="flat"
          class="ml-2"
        >
          {{ aboveAlert.length }} above alert
        </v-chip>
        <v-spacer />
        <span class="text-caption text-medium-contrast mr-2">
          {{ stock.length }} material{{ stock.length === 1 ? '' : 's' }}
        </span>
        <v-icon>{{ stockOpen ? 'mdi-chevron-up' : 'mdi-chevron-down' }}</v-icon>
      </v-card-title>
      <v-expand-transition>
        <div v-show="stockOpen">
          <v-divider />
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
                  v-if="s.below_alert"
                  color="error"
                  size="x-small"
                  variant="tonal"
                >
                  LOW
                </v-chip>
              </template>
            </v-list-item>
          </v-list>
          <div class="text-caption text-medium-contrast pa-3 pt-2">
            Red = below the alert quantity · green = at or above it. Tap a
            material to open its batches.
          </div>
        </div>
      </v-expand-transition>
    </v-card>

    <!-- Summary — MMM, YYYY: the tiles drive the chart area below (user request). -->
    <v-card>
      <v-card-title class="d-flex align-center">
        <v-icon start>mdi-chart-box</v-icon>
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
              <v-card-text class="text-center">
                <div class="text-h5 font-weight-bold">{{ t.value }}</div>
                <div class="text-caption">{{ t.label }}</div>
                <div v-if="t.note" class="text-caption">{{ t.note }}</div>
                <div class="text-caption font-italic">{{ t.caption }}</div>
              </v-card-text>
            </v-card>
          </v-col>
        </v-row>

        <!-- Chart area: populated by the tile selected above (user request). -->
        <div class="d-flex flex-wrap align-center mt-4">
          <div class="text-subtitle-2">{{ chart.title }}</div>
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
        />
        <EmptyState v-else :text="chart.empty" icon="mdi-chart-bar" />

        <!-- Inward material this month — the line items are editable in place. -->
        <div class="text-subtitle-2 mt-4 mb-1">
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
                  <span class="text-caption text-medium-contrast" style="width: 92px">
                    {{ item.date }}
                  </span>
                  <span class="text-body-2">
                    {{ num(item.quantity_received) }} {{ row.unit }}
                  </span>
                  <span class="text-caption text-medium-contrast">
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
              <div class="text-caption text-medium-contrast">
                Editing a line item recalculates stock in hand, the FIFO cost and
                the vendor's payable. Deliveries already made keep the cost that
                applied when they were entered.
              </div>
            </div>
          </div>
        </div>
        <p v-else class="text-medium-contrast text-body-2">
          No stock arrivals this month.
        </p>
      </v-card-text>
    </v-card>
  </div>
</template>


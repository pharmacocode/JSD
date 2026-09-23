<script setup>
/**
 * Reports / Stats (spec 4.7): per-SKU sold & inward quantities by
 * day/week/month + rolling 30-day average. Chart + data table.
 * No P&L/GST/export in v1 (spec 9).
 */
import { ref, onMounted, watch, computed } from 'vue'
import { api, listify } from '@/api'
import { money, num } from '@/utils/format'
import ChartCanvas from '@/components/ChartCanvas.vue'
import EmptyState from '@/components/EmptyState.vue'

const skus = ref([])
const sku = ref(null)
const granularity = ref('day')
const rows = ref([])
const rolling = ref('0')
const loading = ref(false)
const error = ref('')

onMounted(async () => {
  skus.value = listify(await api.get('/skus/'))
  if (skus.value.length) {
    sku.value = skus.value[0].id
    await load()
  }
})

async function load() {
  if (!sku.value) return
  loading.value = true
  error.value = ''
  try {
    const data = await api.get('/reports/', {
      sku: sku.value,
      granularity: granularity.value,
    })
    rows.value = data.rows
    rolling.value = data.rolling_30d_avg_cases_per_day
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

watch([sku, granularity], load)

const chartLabels = computed(() => rows.value.map((r) => r.bucket))
const chartDatasets = computed(() => [
  {
    label: 'Cases sold',
    data: rows.value.map((r) => Number(r.sold_cases)),
  },
  {
    label: 'Inward (material cases)',
    data: rows.value.map((r) =>
      r.inward.reduce((s, i) => s + Number(i.quantity), 0)
    ),
  },
])

function inwardSummary(row) {
  if (!row.inward.length) return '—'
  return row.inward.map((i) => `${i.material}: ${num(i.quantity)} ${i.unit}`).join(', ')
}
</script>

<template>
  <div>
    <v-card class="mb-3">
      <v-card-title>
        <v-icon start>mdi-chart-line</v-icon>
        Sold & inward stats
      </v-card-title>
      <v-divider />
      <v-card-text>
        <v-select
          v-model="sku"
          :items="skus"
          item-title="description"
          item-value="id"
          label="SKU"
          :loading="!skus.length"
        />
        <v-btn-toggle v-model="granularity" mandatory density="compact" color="primary">
          <v-btn value="day">Day</v-btn>
          <v-btn value="week">Week</v-btn>
          <v-btn value="month">Month</v-btn>
        </v-btn-toggle>

        <v-alert v-if="error" type="error" density="compact" class="mt-3">
          {{ error }}
        </v-alert>

        <v-sheet v-if="rows.length" class="mt-4">
          <ChartCanvas
            type="line"
            :labels="chartLabels"
            :datasets="chartDatasets"
            :height="240"
          />
        </v-sheet>
        <EmptyState
          v-else-if="!loading"
          icon="mdi-chart-timeline-variant"
          text="No data for this SKU yet."
        />

        <v-card variant="tonal" color="primary" class="mt-3" v-if="rows.length">
          <v-card-text class="text-center">
            <div class="text-h5 font-weight-bold">{{ rolling }}</div>
            <div class="text-caption">
              Rolling 30-day average — cases sold per day
            </div>
          </v-card-text>
        </v-card>
      </v-card-text>
    </v-card>

    <!-- Data table beneath the chart (spec 4.7) -->
    <v-card v-if="rows.length">
      <v-card-title class="text-subtitle-1">Data</v-card-title>
      <v-divider />
      <v-table density="compact">
        <thead>
          <tr>
            <th>{{ granularity }}</th>
            <th class="text-right">Sold (cases)</th>
            <th class="text-right">Revenue</th>
            <th>Inward</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.bucket">
            <td>{{ r.bucket }}</td>
            <td class="text-right">{{ num(r.sold_cases) }}</td>
            <td class="text-right">{{ money(r.revenue) }}</td>
            <td class="text-caption">{{ inwardSummary(r) }}</td>
          </tr>
        </tbody>
      </v-table>
    </v-card>
  </div>
</template>

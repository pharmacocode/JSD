<script setup>
/**
 * Home (spec 4.1): FAB for + Add Delivery, secondary + Enter Arrived Stock,
 * Stock in Hand panel with alert highlighting, monthly statistics cards.
 */
import { ref, watch, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, num, monthLabel } from '@/utils/format'
import MonthPicker from '@/components/MonthPicker.vue'
import ChartCanvas from '@/components/ChartCanvas.vue'
import EmptyState from '@/components/EmptyState.vue'

const router = useRouter()
const ui = useUiStore()
const loading = ref(true)
const data = ref({ stock: [], stats: {} })
const error = ref('')

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

const soldLabels = computed(
  () => stats.value.cases_sold_per_sku?.map((r) => r.sku) || []
)
const soldData = computed(
  () => stats.value.cases_sold_per_sku?.map((r) => Number(r.cases)) || []
)
const topLabels = computed(
  () => stats.value.top_clients?.map((r) => r.client) || []
)
const topData = computed(
  () => stats.value.top_clients?.map((r) => Number(r.revenue)) || []
)
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

    <!-- Stock in Hand panel -->
    <v-card class="mb-4" :loading="loading">
      <v-card-title class="d-flex align-center">
        <v-icon start>mdi-package-variant-closed</v-icon>
        Stock in Hand
        <v-spacer />
        <v-chip v-if="belowAlert.length" color="error" size="small" variant="flat">
          {{ belowAlert.length }} low
        </v-chip>
      </v-card-title>
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
          :subtitle="s.client_name ? `Client: ${s.client_name}` : s.category"
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
            <v-chip v-if="s.below_alert" color="error" size="x-small" variant="tonal">
              LOW (alert {{ num(s.stock_alert_qty) }})
            </v-chip>
          </template>
        </v-list-item>
      </v-list>
    </v-card>

    <!-- Monthly statistics panel -->
    <v-card>
      <v-card-title>
        <v-icon start>mdi-chart-box</v-icon>
        Statistics — {{ monthLabel(ui.month) }}
      </v-card-title>
      <v-divider />
      <v-card-text>
        <v-row>
          <v-col cols="6" md="3">
            <v-card color="primary" variant="tonal">
              <v-card-text class="text-center">
                <div class="text-h5 font-weight-bold">
                  {{ num(stats.total_cases, 0) }}
                </div>
                <div class="text-caption">Cases sold</div>
              </v-card-text>
            </v-card>
          </v-col>
          <v-col cols="6" md="3">
            <v-card color="success" variant="tonal">
              <v-card-text class="text-center">
                <div class="text-h5 font-weight-bold">
                  {{ money(stats.total_revenue) }}
                </div>
                <div class="text-caption">Revenue</div>
              </v-card-text>
            </v-card>
          </v-col>
          <v-col cols="6" md="3">
            <v-card color="secondary" variant="tonal">
              <v-card-text class="text-center">
                <div class="text-h5 font-weight-bold">
                  {{ money(stats.total_overhead) }}
                </div>
                <div class="text-caption">Overhead</div>
              </v-card-text>
            </v-card>
          </v-col>
          <v-col cols="6" md="3">
            <v-card color="warning" variant="tonal">
              <v-card-text class="text-center">
                <div class="text-h5 font-weight-bold">
                  {{ money(stats.overhead_per_case) }}
                </div>
                <div class="text-caption">OH / case (live)</div>
              </v-card-text>
            </v-card>
          </v-col>
        </v-row>

        <v-row class="mt-2">
          <v-col cols="12" md="6">
            <div class="text-subtitle-2 mb-1">Cases sold per SKU</div>
            <ChartCanvas
              v-if="soldLabels.length"
              type="bar"
              :labels="soldLabels"
              :datasets="[{ label: 'Cases', data: soldData }]"
              :height="200"
            />
            <EmptyState v-else text="No sales this month yet." icon="mdi-cart" />
          </v-col>
          <v-col cols="12" md="6">
            <div class="text-subtitle-2 mb-1">Top clients by revenue</div>
            <ChartCanvas
              v-if="topLabels.length"
              type="doughnut"
              :labels="topLabels"
              :datasets="[{ label: 'Revenue', data: topData }]"
              :height="200"
            />
            <EmptyState v-else text="No client revenue this month." icon="mdi-account" />
          </v-col>
          <v-col cols="12">
            <div class="text-subtitle-2 mb-1">Inward material (this month)</div>
            <v-table v-if="stats.inward_materials?.length" density="compact">
              <thead>
                <tr>
                  <th>Material</th>
                  <th class="text-right">Received</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in stats.inward_materials" :key="r.material_id">
                  <td>{{ r.material }}</td>
                  <td class="text-right">{{ num(r.quantity) }} {{ r.unit }}</td>
                </tr>
              </tbody>
            </v-table>
            <p v-else class="text-medium-contrast text-body-2">
              No stock arrivals this month.
            </p>
          </v-col>
        </v-row>
      </v-card-text>
    </v-card>
  </div>
</template>


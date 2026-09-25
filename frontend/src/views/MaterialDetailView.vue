<script setup>
/**
 * Material detail (spec 4.6): FIFO batch queue oldest-first with remaining
 * qty per batch, plus adjustment shortcut (4.4).
 */
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api'
import { num } from '@/utils/format'
import AuditBadge from '@/components/AuditBadge.vue'
import EmptyState from '@/components/EmptyState.vue'

const route = useRoute()
const router = useRouter()
const id = route.params.id

const material = ref(null)
const batches = ref([])
const stock = ref('0')
const includeDeleted = ref(false)
const loading = ref(true)

async function load() {
  loading.value = true
  try {
    material.value = await api.get(`/materials/${id}/`)
    const data = await api.get(`/materials/${id}/batches/`, {
      include_deleted: includeDeleted.value ? '1' : '',
    })
    batches.value = data.batches
    stock.value = data.stock_in_hand
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>

<template>
  <div v-if="material">
    <v-card class="mb-3">
      <v-card-title class="d-flex align-center">
        {{ material.name }}
        <v-spacer />
        <v-chip :color="material.is_below_alert ? 'error' : 'success'" variant="flat">
          {{ num(stock) }} {{ material.unit_of_measure }}
        </v-chip>
      </v-card-title>
      <v-card-text class="text-body-2">
        {{ material.category }}
        <template v-if="material.client_name"> · {{ material.client_name }}</template>
        · Vendor: {{ material.vendor_name || '—' }} · Alert at
        {{ num(material.stock_alert_qty) }} {{ material.unit_of_measure }} · Wastage
        {{ material.wastage_percent }}%
      </v-card-text>
      <v-card-actions>
        <v-btn
          color="primary"
          variant="tonal"
          prepend-icon="mdi-scale-balance"
          :to="{ path: '/stock/adjust', query: { material: id } }"
        >
          Adjust stock
        </v-btn>
        <v-btn
          color="secondary"
          variant="tonal"
          prepend-icon="mdi-truck-plus"
          :to="{ path: '/stock/arrived' }"
        >
          Arrived stock
        </v-btn>
      </v-card-actions>
    </v-card>

    <v-card>
      <v-card-title class="d-flex align-center text-subtitle-1">
        Batch history (FIFO — oldest first)
        <v-spacer />
        <v-switch
          v-model="includeDeleted"
          density="compact"
          label="Audit trail"
          hide-details
          class="ml-auto"
          @update:model-value="load"
        />
      </v-card-title>
      <v-divider />
      <EmptyState
        v-if="!loading && !batches.length"
        icon="mdi-layers-triple"
        text="No batches yet — record arrived stock first."
        action="Enter Arrived Stock"
        @action="router.push('/stock/arrived')"
      />
      <v-table v-else density="compact">
        <thead>
          <tr>
            <th>Arrival</th>
            <th class="text-right">Received (cases)</th>
            <th class="text-right">Remaining</th>
            <th class="text-right">₹ / case</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="b in batches"
            :key="b.id"
            :class="b.is_deleted ? 'text-medium-emphasis' : ''"
          >
            <td>{{ b.arrival_date }}</td>
            <td class="text-right">{{ num(b.quantity_received) }}</td>
            <td
              class="text-right font-weight-bold"
              :class="Number(b.quantity_remaining) < 0 ? 'text-error' : ''"
            >
              {{ num(b.quantity_remaining) }}
            </td>
            <td class="text-right">₹{{ b.price_per_unit }}</td>
            <td class="text-right">
              <AuditBadge :record="b" />
            </td>
          </tr>
        </tbody>
      </v-table>
    </v-card>
  </div>

  <v-progress-circular v-else-if="loading" indeterminate class="d-block mx-auto mt-8" />
</template>

<script setup>
/**
 * Stock Adjustment (spec 4.4): signed adjustment with mandatory reason.
 * Positive = add (zero-price batch), negative = remove (FIFO consumption).
 * Accessed from Stock panel / Masters.
 */
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, listify } from '@/api'
import { useUiStore } from '@/stores/ui'
import { today, num } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()

const materials = ref([])
const material = ref(null)
const quantity = ref(0)
const reason = ref('')
const date = ref(today())
const loading = ref(false)
const error = ref('')
const history = ref([])
const result = ref(null)

onMounted(async () => {
  try {
    materials.value = listify(await api.get('/materials/'))
    if (route.query.material) {
      const m = materials.value.find(
        (x) => x.id === Number(route.query.material)
      )
      if (m) {
        material.value = m
        loadHistory()
      }
    }
  } catch (e) {
    error.value = e.message
  }
})

async function loadHistory() {
  if (!material.value) return
  history.value = listify(
    await api.get(`/materials/${material.value.id}/adjustments/`)
  )
}

async function save() {
  error.value = ''
  if (!material.value || !reason.value.trim()) {
    error.value = 'A reason is required for stock adjustments.'
    return
  }
  loading.value = true
  try {
    result.value = await api.post('/adjustments/', {
      material: material.value.id,
      quantity: String(quantity.value),
      reason: reason.value,
      date: date.value,
    })
    ui.notify(
      `Adjusted ${material.value.name} by ${quantity.value} ${material.value.unit_of_measure}`
    )
    quantity.value = 0
    reason.value = ''
    await loadHistory()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div>
    <v-card class="mb-4">
      <v-card-title>
        <v-icon start>mdi-scale-balance</v-icon>
        Stock Adjustment
      </v-card-title>
      <v-divider />
      <v-card-text>
        <v-alert v-if="error" type="error" class="mb-3" density="compact">
          {{ error }}
        </v-alert>
        <v-alert type="info" variant="tonal" density="compact" class="mb-3">
          Adjustments never silently edit FIFO batches: additions enter as a
          new zero-price batch; removals consume the oldest batches — the audit
          trail keeps every change visible.
        </v-alert>

        <v-select
          v-model="material"
          :items="materials"
          item-title="name"
          item-value="id"
          return-object
          label="Material"
          clearable
          @update:model-value="loadHistory"
        />
        <div
          v-if="material"
          class="text-caption mb-2"
          :class="
            Number(material.stock_in_hand) < 0
              ? 'text-error'
              : 'text-medium-emphasis'
          "
        >
          Current stock in hand:
          <strong>
            {{ num(material.stock_in_hand) }} {{ material.unit_of_measure }}
          </strong>
          <span v-if="Number(material.stock_in_hand) < 0">
            — negative: add the missing cases below (+) or record a backdated
            arrival to clear it.
          </span>
        </div>

        <v-text-field
          v-model.number="quantity"
          type="number"
          step="any"
          label="Quantity in cases (positive = add, negative = remove)"
          hint="e.g. +10 to clear negative stock, -2 damaged"
          persistent-hint
        />
        <v-text-field
          v-model="reason"
          label="Reason (required)"
          required
        />
        <v-text-field v-model="date" type="date" label="Date" />

        <v-btn
          color="primary"
          block
          size="large"
          :loading="loading"
          @click="save"
        >
          Apply adjustment
        </v-btn>
      </v-card-text>
    </v-card>

    <v-card v-if="history.length">
      <v-card-title class="text-subtitle-1">Adjustment history</v-card-title>
      <v-divider />
      <v-table density="compact">
        <thead>
          <tr>
            <th>Date</th>
            <th>Qty</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in history" :key="a.id">
            <td>{{ a.date }}</td>
            <td :class="Number(a.quantity) >= 0 ? 'text-success' : 'text-error'">
              {{ a.quantity }}
            </td>
            <td>{{ a.reason }}</td>
          </tr>
        </tbody>
      </v-table>
    </v-card>

    <v-btn
      v-if="result"
      class="mt-4"
      color="success"
      variant="tonal"
      block
      @click="router.push('/')"
    >
      Done — back to Home
    </v-btn>
  </div>
</template>

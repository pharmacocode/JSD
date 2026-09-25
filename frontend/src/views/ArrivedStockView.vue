<script setup>
/**
 * Enter Arrived Stock (spec 4.3): creates a MaterialBatch.
 * Client-specific label materials are grouped by client for clarity.
 */
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api, listify } from '@/api'
import { useUiStore } from '@/stores/ui'
import { today } from '@/utils/format'

const router = useRouter()
const ui = useUiStore()

const materials = ref([])
const material = ref(null)
const qty = ref(null)
const price = ref(null)
const date = ref(today())
const note = ref('')
const loading = ref(false)
const error = ref('')

onMounted(async () => {
  try {
    materials.value = listify(await api.get('/materials/'))
  } catch (e) {
    error.value = e.message
  }
})

// Group labels by client for the dropdown (spec 4.3).
const grouped = computed(() => {
  const groups = {}
  for (const m of materials.value) {
    const key = m.is_client_specific
      ? `Labels — ${m.client_name || 'Unassigned'}`
      : 'General'
    ;(groups[key] ||= []).push(m)
  }
  return groups
})

async function save() {
  error.value = ''
  if (!material.value || !qty.value || !price.value) {
    error.value = 'Material, quantity and price are required.'
    return
  }
  loading.value = true
  try {
    await api.post('/batches/', {
      material: material.value.id,
      quantity_received: String(qty.value),
      price_per_unit: String(price.value),
      arrival_date: date.value,
      note: note.value,
    })
    ui.notify(`Stock received: ${qty.value} × ${material.value.name}`)
    router.push('/')
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <v-card>
    <v-card-title class="d-flex align-center text-subtitle-1 font-weight-bold">
      <v-icon start color="primary">mdi-truck-plus</v-icon>
      Enter Arrived Stock
    </v-card-title>
    <v-divider />
    <v-card-text>
      <v-alert v-if="error" type="error" class="mb-3" density="compact">
        {{ error }}
      </v-alert>

      <v-select
        v-model="material"
        :items="materials"
        item-title="name"
        item-value="id"
        label="Material"
        :loading="!materials.length"
        return-object
        clearable
        hint="Client-specific label stock is grouped by client in this list."
        persistent-hint
      >
        <template #item="{ item, props }">
          <v-list-item
            v-bind="props"
            :subtitle="`${item.raw.category}${
              item.raw.client_name ? ' · ' + item.raw.client_name : ''
            } · in hand: ${item.raw.stock_in_hand}`"
          />
        </template>
      </v-select>

      <!-- Section label: same 12px uppercase treatment used across the app. -->
      <div
        class="text-caption font-weight-bold text-uppercase text-medium-emphasis mt-4 mb-1"
      >
        Arrival details
      </div>

      <v-text-field
        v-model.number="qty"
        type="number"
        min="0"
        step="any"
        label="Quantity received"
        :suffix="material?.unit_of_measure || 'cases'"
      />
      <!-- Short label; the "this batch only" caveat moved into the hint. -->
      <v-text-field
        v-model.number="price"
        type="number"
        min="0"
        step="0.01"
        label="Price per case"
        prefix="₹"
        hint="Cost of this batch only — FIFO uses it from here on."
      />
      <v-text-field
        v-model="date"
        type="date"
        label="Arrival date"
        hint="Backdate it when the stock actually arrived earlier — e.g. to clear negative stock."
        persistent-hint
      />
      <v-text-field v-model="note" label="Note (optional)" />

      <div class="d-flex ga-2 mt-2">
        <v-btn variant="tonal" size="large" @click="router.push('/')">
          Cancel
        </v-btn>
        <v-btn
          color="success"
          size="large"
          class="flex-grow-1"
          :loading="loading"
          prepend-icon="mdi-check"
          @click="save"
        >
          Save arrival
        </v-btn>
      </div>
    </v-card-text>
  </v-card>
</template>

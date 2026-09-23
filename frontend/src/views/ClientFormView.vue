<script setup>
/**
 * Client create/edit form (spec 4.5): name, Google Maps URL, contact,
 * opening pending amount (create only), preferred SKUs + prices (multi-add).
 */
import { ref, onMounted, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, listify } from '@/api'
import { useUiStore } from '@/stores/ui'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()
const isEdit = computed(() => !!route.params.id)

const form = ref({
  name: '',
  google_maps_url: '',
  contact_number: '',
  opening_pending_amount: 0,
})
const allSkus = ref([])
const prices = ref([]) // [{sku, selling_price_per_case}]
const loading = ref(false)
const saving = ref(false)
const error = ref('')

onMounted(async () => {
  loading.value = true
  try {
    allSkus.value = listify(await api.get('/skus/'))
    if (isEdit.value) {
      const c = await api.get(`/clients/${route.params.id}/`)
      form.value = {
        name: c.name,
        google_maps_url: c.google_maps_url,
        contact_number: c.contact_number,
        opening_pending_amount: Number(c.opening_pending_amount),
      }
      prices.value = (c.sku_prices || []).map((p) => ({
        sku: p.sku,
        selling_price_per_case: Number(p.selling_price_per_case),
      }))
    }
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
})

function addPriceRow() {
  prices.value.push({ sku: null, selling_price_per_case: 0 })
}

async function save() {
  error.value = ''
  if (!form.value.name.trim()) {
    error.value = 'Client name is required.'
    return
  }
  saving.value = true
  try {
    let clientId = route.params.id
    if (isEdit.value) {
      await api.put(`/clients/${clientId}/`, form.value)
    } else {
      const created = await api.post('/clients/', form.value)
      clientId = created.id
    }
    // Sync preferred SKU prices: PUT existing, POST new, DELETE removed.
    const existing = listify(await api.get('/client-prices/', { client: clientId }))
    const keepIds = new Set()
    for (const row of prices.value) {
      if (!row.sku) continue
      const match = existing.find((p) => p.sku === row.sku)
      if (match) {
        if (String(match.selling_price_per_case) !== String(row.selling_price_per_case)) {
          await api.put(`/client-prices/${match.id}/`, {
            client: clientId,
            sku: row.sku,
            selling_price_per_case: String(row.selling_price_per_case),
          })
        }
        keepIds.add(match.id)
      } else {
        const created = await api.post('/client-prices/', {
          client: clientId,
          sku: row.sku,
          selling_price_per_case: String(row.selling_price_per_case),
        })
        keepIds.add(created.id)
      }
    }
    for (const p of existing) {
      if (!keepIds.has(p.id)) await api.del(`/client-prices/${p.id}/`)
    }
    ui.notify(`Client "${form.value.name}" saved`)
    router.push(`/clients/${clientId}`)
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <v-card :loading="loading">
    <v-card-title>
      {{ isEdit ? 'Edit client' : 'New client' }}
    </v-card-title>
    <v-divider />
    <v-card-text>
      <v-alert v-if="error" type="error" class="mb-3" density="compact">
        {{ error }}
      </v-alert>

      <v-text-field v-model="form.name" label="Name *" />
      <v-text-field
        v-model="form.google_maps_url"
        label="Google Maps URL"
        hint="Opens in a new tab — no embedded map"
        persistent-hint
      />
      <v-text-field v-model="form.contact_number" label="Contact number" />
      <v-text-field
        v-if="!isEdit"
        v-model.number="form.opening_pending_amount"
        type="number"
        prefix="₹"
        label="Opening pending amount (one-time, pre-app balance)"
      />

      <div class="d-flex align-center mt-4 mb-2">
        <span class="text-subtitle-2">Preferred SKUs & prices</span>
        <v-spacer />
        <v-btn size="small" variant="tonal" prepend-icon="mdi-plus" @click="addPriceRow">
          Add SKU
        </v-btn>
      </div>
      <div
        v-for="(row, i) in prices"
        :key="i"
        class="d-flex ga-2 align-center mb-2"
      >
        <v-select
          v-model="row.sku"
          :items="allSkus"
          item-title="description"
          item-value="id"
          label="SKU"
          density="compact"
          class="flex-grow-1"
        />
        <v-text-field
          v-model.number="row.selling_price_per_case"
          type="number"
          prefix="₹"
          label="₹/case"
          density="compact"
          style="max-width: 140px"
        />
        <v-btn
          icon="mdi-close"
          size="small"
          variant="text"
          color="error"
          @click="prices.splice(i, 1)"
        />
      </div>

      <v-btn
        color="primary"
        block
        size="large"
        class="mt-4"
        :loading="saving"
        @click="save"
      >
        Save client
      </v-btn>
    </v-card-text>
  </v-card>
</template>

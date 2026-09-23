<script setup>
/**
 * MVP Master (spec 4.6): material list + add/edit with inline stock in hand.
 */
import { ref, watch, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api, listify } from '@/api'
import { useUiStore } from '@/stores/ui'
import { num } from '@/utils/format'
import EmptyState from '@/components/EmptyState.vue'

const router = useRouter()
const ui = useUiStore()

const search = ref('')
const materials = ref([])
const loading = ref(true)
const dialog = ref(false)
const editing = ref(null)
const clients = ref([])
const vendors = ref([])
const saving = ref(false)
const error = ref('')

const blank = () => ({
  id: null,
  name: '',
  category: 'Other',
  is_client_specific: false,
  client: null,
  vendor: null,
  current_price_per_unit: 0,
  stock_alert_qty: 0,
  wastage_percent: 0,
  unit_of_measure: 'cases',
})
const form = ref(blank())

async function load() {
  loading.value = true
  try {
    materials.value = listify(
      await api.get('/materials/', { search: search.value })
    )
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await load()
  clients.value = listify(await api.get('/clients/'))
  vendors.value = listify(await api.get('/vendors/'))
})

let t = null
watch(search, () => {
  clearTimeout(t)
  t = setTimeout(load, 250)
})

function openCreate() {
  editing.value = false
  form.value = blank()
  error.value = ''
  dialog.value = true
}

function openEdit(m) {
  editing.value = true
  form.value = { ...m, client: m.client }
  error.value = ''
  dialog.value = true
}

async function save() {
  error.value = ''
  if (!form.value.name.trim()) {
    error.value = 'Name is required.'
    return
  }
  if (form.value.is_client_specific && !form.value.client) {
    error.value = 'Client-specific materials need a client.'
    return
  }
  saving.value = true
  try {
    const payload = { ...form.value }
    delete payload.stock_in_hand
    delete payload.is_below_alert
    delete payload.client_name
    delete payload.vendor_name
    delete payload.created_at
    if (form.value.id) await api.put(`/materials/${form.value.id}/`, payload)
    else await api.post('/materials/', payload)
    ui.notify(`Material "${form.value.name}" saved`)
    dialog.value = false
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div>
    <v-text-field
      v-model="search"
      prepend-inner-icon="mdi-magnify"
      label="Search materials…"
      clearable
      class="mb-2"
    />
    <v-btn color="primary" block class="mb-3" prepend-icon="mdi-plus" @click="openCreate">
      Add material
    </v-btn>

    <v-card :loading="loading">
      <EmptyState
        v-if="!loading && !materials.length"
        icon="mdi-package-variant"
        text="No materials yet — tap + to add one."
      />
      <v-list v-else density="compact">
        <v-list-item
          v-for="m in materials"
          :key="m.id"
          :title="m.name"
          :subtitle="`${m.category}${m.client_name ? ' · ' + m.client_name : ''} · ${m.vendor_name || 'no vendor'}`"
          :to="`/masters/materials/${m.id}`"
        >
          <template #append>
            <div class="text-right mr-2">
              <v-chip
                :color="m.is_below_alert ? 'error' : 'success'"
                size="small"
                variant="flat"
              >
                {{ num(m.stock_in_hand) }} {{ m.unit_of_measure }}
              </v-chip>
              <div v-if="m.is_below_alert" class="text-caption text-error">
                below alert {{ num(m.stock_alert_qty) }}
              </div>
            </div>
            <v-btn
              icon="mdi-pencil"
              size="small"
              variant="text"
              @click.prevent="openEdit(m)"
            />
          </template>
        </v-list-item>
      </v-list>
    </v-card>

    <v-dialog v-model="dialog" max-width="480">
      <v-card>
        <v-card-title>{{ editing ? 'Edit' : 'Add' }} material</v-card-title>
        <v-card-text>
          <v-alert v-if="error" type="error" density="compact" class="mb-2">
            {{ error }}
          </v-alert>
          <v-text-field v-model="form.name" label="Name *" />
          <v-select
            v-model="form.category"
            :items="['Bottle', 'Cap', 'Label', 'Other']"
            label="Category"
          />
          <v-switch
            v-model="form.is_client_specific"
            color="primary"
            label="Client-specific (custom labels)"
            density="compact"
          />
          <v-select
            v-if="form.is_client_specific"
            v-model="form.client"
            :items="clients"
            item-title="name"
            item-value="id"
            label="Client *"
          />
          <v-select
            v-model="form.vendor"
            :items="vendors"
            item-title="name"
            item-value="id"
            label="Vendor"
            clearable
            hint="Vendor list is managed in Masters → Vendors"
            persistent-hint
          />
          <v-text-field
            v-model.number="form.current_price_per_unit"
            type="number"
            prefix="₹"
            label="Display price per case (costing uses batches)"
          />
          <v-text-field
            v-model.number="form.stock_alert_qty"
            type="number"
            label="Stock alert qty (cases)"
          />
          <v-text-field
            v-model.number="form.wastage_percent"
            type="number"
            label="Wastage % (this material)"
          />
          <v-text-field
            v-model="form.unit_of_measure"
            label="Unit (cases — quantities are case-based)"
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="dialog = false">Cancel</v-btn>
          <v-btn color="primary" :loading="saving" @click="save">Save</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

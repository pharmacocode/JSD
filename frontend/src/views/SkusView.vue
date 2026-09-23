<script setup>
/** SKU Master list + create/edit (spec 4.6). */
import { ref, watch, onMounted } from 'vue'
import { api, listify } from '@/api'
import { useUiStore } from '@/stores/ui'
import { num } from '@/utils/format'
import EmptyState from '@/components/EmptyState.vue'

const ui = useUiStore()
const search = ref('')
const skus = ref([])
const loading = ref(true)
const dialog = ref(false)
const saving = ref(false)
const error = ref('')

const blank = () => ({ id: null, description: '', qty_per_case: 24, volume_ml: 500 })
const form = ref(blank())

async function load() {
  loading.value = true
  try {
    skus.value = listify(await api.get('/skus/', { search: search.value }))
  } finally {
    loading.value = false
  }
}
onMounted(load)

let t = null
watch(search, () => {
  clearTimeout(t)
  t = setTimeout(load, 250)
})

function openCreate() {
  form.value = blank()
  error.value = ''
  dialog.value = true
}

function openEdit(s) {
  form.value = { ...s }
  error.value = ''
  dialog.value = true
}

async function save() {
  error.value = ''
  if (!form.value.description.trim()) {
    error.value = 'Description is required.'
    return
  }
  saving.value = true
  try {
    const payload = {
      description: form.value.description,
      qty_per_case: String(form.value.qty_per_case),
      volume_ml: Number(form.value.volume_ml),
    }
    if (form.value.id) await api.put(`/skus/${form.value.id}/`, payload)
    else await api.post('/skus/', payload)
    ui.notify(`SKU "${form.value.description}" saved`)
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
      label="Search SKUs…"
      clearable
      class="mb-2"
    />
    <v-btn color="primary" block class="mb-3" prepend-icon="mdi-plus" @click="openCreate">
      Add SKU
    </v-btn>

    <v-card :loading="loading">
      <EmptyState
        v-if="!loading && !skus.length"
        icon="mdi-barcode"
        text="No SKUs yet — tap + to add one."
      />
      <v-list v-else density="compact">
        <v-list-item
          v-for="s in skus"
          :key="s.id"
          :title="s.description"
          :subtitle="`${num(s.qty_per_case)} per case · ${s.volume_ml} mL`"
          :to="`/masters/skus/${s.id}`"
        >
          <template #append>
            <v-btn
              icon="mdi-pencil"
              size="small"
              variant="text"
              @click.prevent="openEdit(s)"
            />
          </template>
        </v-list-item>
      </v-list>
    </v-card>

    <v-dialog v-model="dialog" max-width="420">
      <v-card>
        <v-card-title>{{ form.id ? 'Edit' : 'Add' }} SKU</v-card-title>
        <v-card-text>
          <v-alert v-if="error" type="error" density="compact" class="mb-2">
            {{ error }}
          </v-alert>
          <v-text-field v-model="form.description" label="Description *" />
          <v-text-field
            v-model.number="form.qty_per_case"
            type="number"
            label="Quantity per case"
          />
          <v-text-field v-model.number="form.volume_ml" type="number" label="Volume (mL)" />
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

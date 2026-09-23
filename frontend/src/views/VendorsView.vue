<script setup>
/**
 * Vendor master (user request): create the vendor list, see contact number
 * and the amount we owe each vendor (arrived stock purchases minus payments).
 * Vendors are selected in the Material form (MVP Master).
 */
import { ref, watch, onMounted } from 'vue'
import { api, listify } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, today } from '@/utils/format'
import EmptyState from '@/components/EmptyState.vue'

const ui = useUiStore()

const search = ref('')
const vendors = ref([])
const loading = ref(true)
const error = ref('')

const dialog = ref(false)
const form = ref({ id: null, name: '', contact_number: '' })
const saving = ref(false)

const detail = ref(null) // vendor being viewed
const payments = ref([])
const payDialog = ref(false)
const payForm = ref({ amount: null, date: today(), note: '' })

async function load() {
  loading.value = true
  try {
    vendors.value = listify(await api.get('/vendors/', { search: search.value }))
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
  form.value = { id: null, name: '', contact_number: '' }
  error.value = ''
  dialog.value = true
}

function openEdit(v) {
  form.value = { id: v.id, name: v.name, contact_number: v.contact_number }
  error.value = ''
  dialog.value = true
}

async function save() {
  error.value = ''
  if (!form.value.name.trim()) {
    error.value = 'Vendor name is required.'
    return
  }
  saving.value = true
  try {
    const payload = {
      name: form.value.name,
      contact_number: form.value.contact_number,
    }
    if (form.value.id) await api.put(`/vendors/${form.value.id}/`, payload)
    else await api.post('/vendors/', payload)
    ui.notify(`Vendor "${form.value.name}" saved`)
    dialog.value = false
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

async function openDetail(v) {
  const data = await api.get(`/vendors/${v.id}/payments/`)
  detail.value = data.vendor
  payments.value = data.payments
}

async function savePayment() {
  error.value = ''
  if (!payForm.value.amount || Number(payForm.value.amount) <= 0) {
    error.value = 'Enter a positive amount.'
    return
  }
  saving.value = true
  try {
    await api.post(`/vendors/${detail.value.id}/payments/`, {
      amount: String(payForm.value.amount),
      date: payForm.value.date,
      note: payForm.value.note,
    })
    ui.notify(`Payment of ${money(payForm.value.amount)} recorded`)
    payDialog.value = false
    payForm.value = { amount: null, date: today(), note: '' }
    await openDetail({ id: detail.value.id })
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
      label="Search vendors…"
      clearable
      class="mb-2"
    />
    <v-btn color="primary" block class="mb-3" prepend-icon="mdi-plus" @click="openCreate">
      Add vendor
    </v-btn>

    <v-card :loading="loading">
      <EmptyState
        v-if="!loading && !vendors.length"
        icon="mdi-store"
        text="No vendors yet — tap + to add one."
      />
      <v-list v-else density="compact">
        <v-list-item
          v-for="v in vendors"
          :key="v.id"
          :title="v.name"
          :subtitle="`${v.contact_number || 'no contact'} · ${v.material_count} material(s)`"
          @click="openDetail(v)"
        >
          <template #append>
            <div class="text-right mr-1">
              <div class="text-caption text-medium-contrast">we owe</div>
              <v-chip
                :color="Number(v.amount_owed) > 0 ? 'error' : 'success'"
                size="small"
                variant="flat"
              >
                {{ money(v.amount_owed) }}
              </v-chip>
            </div>
            <v-btn icon="mdi-pencil" size="small" variant="text" @click.stop="openEdit(v)" />
          </template>
        </v-list-item>
      </v-list>
    </v-card>

    <!-- Create / edit vendor -->
    <v-dialog v-model="dialog" max-width="420">
      <v-card>
        <v-card-title>{{ form.id ? 'Edit' : 'Add' }} vendor</v-card-title>
        <v-card-text>
          <v-alert v-if="error" type="error" density="compact" class="mb-2">
            {{ error }}
          </v-alert>
          <v-text-field v-model="form.name" label="Vendor name *" />
          <v-text-field
            v-model="form.contact_number"
            label="Contact number"
            prefix="☎"
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="dialog = false">Cancel</v-btn>
          <v-btn color="primary" :loading="saving" @click="save">Save</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Vendor detail: totals, payments, record payment -->
    <v-dialog
      :model-value="!!detail"
      max-width="520"
      @update:model-value="(val) => !val && (detail = null)"
    >
      <v-card v-if="detail">
        <v-card-title>{{ detail.name }}</v-card-title>
        <v-card-subtitle>
          Contact: {{ detail.contact_number || '—' }}
        </v-card-subtitle>
        <v-card-text>
          <v-row dense class="mb-2">
            <v-col cols="4">
              <v-sheet color="grey-lighten-3" rounded="lg" class="pa-3 text-center">
                <div class="text-caption">Purchased</div>
                <div class="font-weight-bold">{{ money(detail.total_purchased) }}</div>
              </v-sheet>
            </v-col>
            <v-col cols="4">
              <v-sheet color="grey-lighten-3" rounded="lg" class="pa-3 text-center">
                <div class="text-caption">Paid</div>
                <div class="font-weight-bold">{{ money(detail.total_paid) }}</div>
              </v-sheet>
            </v-col>
            <v-col cols="4">
              <v-sheet
                :color="Number(detail.amount_owed) > 0 ? 'error' : 'success'"
                rounded="lg"
                class="pa-3 text-center"
              >
                <div class="text-caption" style="color: #fff">We owe</div>
                <div class="font-weight-bold" style="color: #fff">
                  {{ money(detail.amount_owed) }}
                </div>
              </v-sheet>
            </v-col>
          </v-row>

          <v-btn
            color="success"
            block
            prepend-icon="mdi-cash-plus"
            class="mb-3"
            @click="payDialog = true"
          >
            Record payment to vendor
          </v-btn>

          <div class="text-subtitle-2 mb-1">Payment history</div>
          <v-table density="compact">
            <tbody>
              <tr v-for="p in payments" :key="p.id">
                <td>{{ p.date }}</td>
                <td>{{ p.note || '—' }}</td>
                <td class="text-right">{{ money(p.amount) }}</td>
              </tr>
              <tr v-if="!payments.length">
                <td colspan="3" class="text-medium-contrast">
                  No payments recorded yet.
                </td>
              </tr>
            </tbody>
          </v-table>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="detail = null">Close</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Record payment -->
    <v-dialog v-model="payDialog" max-width="400">
      <v-card>
        <v-card-title>Record payment to vendor</v-card-title>
        <v-card-text>
          <v-text-field
            v-model.number="payForm.amount"
            type="number"
            prefix="₹"
            label="Amount paid"
          />
          <v-text-field v-model="payForm.date" type="date" label="Date" />
          <v-text-field v-model="payForm.note" label="Note (e.g. NEFT / cash)" />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="payDialog = false">Cancel</v-btn>
          <v-btn color="success" :loading="saving" @click="savePayment">Save</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>


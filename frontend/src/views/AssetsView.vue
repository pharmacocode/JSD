<script setup>
/**
 * Asset & Loan Master (user request): add the business's fixed assets —
 * labelled depreciating or non-depreciating, the life details driving the
 * value estimate — and the loans behind the Liabilities side, with their
 * EMI, payments and profit transfers.
 */
import { computed, onActivated, onMounted, ref } from 'vue'
import { api } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, monthLabel, today } from '@/utils/format'
import AssetDialog from '@/components/AssetDialog.vue'
import LoanDialog from '@/components/LoanDialog.vue'
import EmptyState from '@/components/EmptyState.vue'

const ui = useUiStore()
const tab = ref('assets')
const loading = ref(true)
const error = ref('')
const assets = ref([])
const loans = ref([])

const assetDialog = ref(false)
const editingAsset = ref(null)
const loanDialog = ref(false)
const editingLoan = ref(null)

// Loan detail + record payment (EMI or any other movement).
const detail = ref(null)
const payments = ref([])
const payDialog = ref(false)
const payForm = ref({ amount: null, date: today(), note: 'EMI' })

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [a, l] = await Promise.all([api.get('/assets/'), api.get('/loans/')])
    assets.value = a.results || a
    loans.value = l.results || l
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
onActivated(load)

const totals = computed(() => ({
  assets: assets.value.reduce((s, a) => s + Number(a.current_value), 0),
  outstanding: loans.value.reduce((s, l) => s + Number(l.outstanding), 0),
}))

// One line under the name: what the asset's value is made of.
function assetSubtitle(a) {
  if (a.kind !== 'DEPRECIATING') return `Non-depreciating · ${money(a.initial_value)}`
  const bits = [`Bought ${monthLabel(a.purchase_month)}`]
  if (a.lifetime_years) bits.push(`${a.lifetime_years} yr life`)
  bits.push(`scrap ${money(a.end_of_life_value)}`)
  return bits.join(' · ')
}

// EMI, rate and tenure in one line under the loan's name.
function loanSubtitle(l) {
  return `EMI ${money(l.emi)} · ${l.annual_interest_rate}% · ${l.duration_months} mo · paid ${money(l.amount_paid)}`
}

// --- Assets ---------------------------------------------------------------
function openCreateAsset() {
  editingAsset.value = null
  assetDialog.value = true
}

function openEditAsset(a) {
  editingAsset.value = a
  assetDialog.value = true
}

async function removeAsset(a) {
  if (!confirm(`Delete asset "${a.name}"?`)) return
  try {
    await api.del(`/assets/${a.id}/`)
    ui.notify('Asset deleted')
    await load()
  } catch (e) {
    error.value = e.message
  }
}

// --- Loans ----------------------------------------------------------------
function openCreateLoan() {
  editingLoan.value = null
  loanDialog.value = true
}

function openEditLoan(l) {
  editingLoan.value = l
  loanDialog.value = true
}

async function removeLoan(l) {
  if (!confirm(`Delete loan "${l.name || 'Loan'}"? Its payments go with it.`))
    return
  try {
    await api.del(`/loans/${l.id}/`)
    ui.notify('Loan deleted')
    await load()
  } catch (e) {
    error.value = e.message
  }
}

async function openDetail(l) {
  const data = await api.get(`/loans/${l.id}/payments/`)
  detail.value = data.loan
  payments.value = data.payments
}

function openPay() {
  payForm.value = {
    amount: Number(detail.value.emi),
    date: today(),
    note: 'EMI',
  }
  payDialog.value = true
}

async function savePayment() {
  error.value = ''
  if (!Number(payForm.value.amount) || Number(payForm.value.amount) <= 0) {
    error.value = 'Enter a positive amount.'
    return
  }
  try {
    await api.post(`/loans/${detail.value.id}/payments/`, {
      amount: String(payForm.value.amount),
      date: payForm.value.date,
      note: payForm.value.note,
    })
    ui.notify('Payment recorded')
    payDialog.value = false
    await openDetail({ id: detail.value.id })
    await load()
  } catch (e) {
    error.value = e.message
  }
}
</script>
<template>
  <div>
    <v-alert v-if="error" type="error" density="compact" class="mb-4">
      {{ error }} — check the API connection.
    </v-alert>

    <v-tabs v-model="tab" color="primary" class="mb-3">
      <v-tab value="assets" prepend-icon="mdi-car-cash">Assets</v-tab>
      <v-tab value="loans" prepend-icon="mdi-bank-outline">Loans</v-tab>
    </v-tabs>

    <v-tabs-window v-model="tab" :loading="loading">
      <!-- Assets: label first (depreciating / non-depreciating), then the
           life details that drive the estimate. -->
      <v-tabs-window-item value="assets">
        <div class="d-flex align-center mb-3">
          <div class="text-body-2 text-medium-emphasis">
            Total today: <strong>{{ money(totals.assets) }}</strong>
          </div>
          <v-spacer />
          <v-btn color="primary" prepend-icon="mdi-plus" @click="openCreateAsset">
            Add asset
          </v-btn>
        </div>
        <EmptyState
          v-if="!assets.length"
          icon="mdi-car-cash"
          text="No assets yet — add your first one (auto, printing machine, savings…)."
          action="Add asset"
          @action="openCreateAsset"
        />
        <v-list v-if="assets.length" density="compact" lines="two">
          <v-list-item
            v-for="a in assets"
            :key="a.id"
            :title="a.name"
            :subtitle="assetSubtitle(a)"
          >
            <template #prepend>
              <v-avatar :color="a.kind === 'DEPRECIATING' ? 'warning' : 'success'" variant="tonal" size="36">
                <v-icon>
                  {{ a.kind === 'DEPRECIATING' ? 'mdi-car-cash' : 'mdi-piggy-bank-outline' }}
                </v-icon>
              </v-avatar>
            </template>
            <template #append>
              <div class="text-right mr-2">
                <div class="font-weight-bold">{{ money(a.current_value) }}</div>
                <div
                  v-if="a.kind === 'DEPRECIATING' && Number(a.current_value) < Number(a.initial_value)"
                  class="text-caption text-medium-emphasis"
                >
                  of {{ money(a.initial_value) }}
                </div>
              </div>
              <v-btn
                icon="mdi-pencil-outline"
                size="small"
                variant="text"
                @click="openEditAsset(a)"
              />
              <v-btn
                icon="mdi-delete-outline"
                size="small"
                variant="text"
                color="error"
                @click="removeAsset(a)"
              />
            </template>
          </v-list-item>
        </v-list>
      </v-tabs-window-item>

      <!-- Loans: EMI terms, the outstanding, and the payment history. -->
      <v-tabs-window-item value="loans">
        <div class="d-flex align-center mb-3">
          <div class="text-body-2 text-medium-emphasis">
            Outstanding: <strong>{{ money(totals.outstanding) }}</strong>
          </div>
          <v-spacer />
          <v-btn color="primary" prepend-icon="mdi-plus" @click="openCreateLoan">
            Add loan
          </v-btn>
        </div>
        <EmptyState
          v-if="!loans.length"
          icon="mdi-bank-outline"
          text="No loans yet — add one to see its EMI and outstanding here."
          action="Add loan"
          @action="openCreateLoan"
        />
        <v-list v-if="loans.length" density="compact" lines="two">
          <v-list-item
            v-for="l in loans"
            :key="l.id"
            :title="l.name || `Loan #${l.id}`"
            :subtitle="loanSubtitle(l)"
            @click="openDetail(l)"
          >
            <template #append>
              <div class="text-right mr-2">
                <div class="font-weight-bold text-error">
                  {{ money(l.outstanding) }}
                </div>
                <div class="text-caption text-medium-emphasis">outstanding</div>
              </div>
              <v-btn
                icon="mdi-history"
                size="small"
                variant="text"
                title="Payments"
                @click.stop="openDetail(l)"
              />
              <v-btn
                icon="mdi-pencil-outline"
                size="small"
                variant="text"
                @click.stop="openEditLoan(l)"
              />
              <v-btn
                icon="mdi-delete-outline"
                size="small"
                variant="text"
                color="error"
                @click.stop="removeLoan(l)"
              />
            </template>
          </v-list-item>
        </v-list>
      </v-tabs-window-item>
    </v-tabs-window>

    <AssetDialog
      v-model="assetDialog"
      :asset="editingAsset"
      @saved="load"
    />
    <LoanDialog v-model="loanDialog" :loan="editingLoan" @saved="load" />

    <!-- Loan detail: terms + every payment + record one (EMI or transfer). -->
    <v-dialog v-model="detail" max-width="460">
      <v-card v-if="detail">
        <v-card-title>
          {{ detail.name || `Loan #${detail.id}` }}
        </v-card-title>
        <v-card-text>
          <v-row dense class="text-center">
            <v-col cols="6">
              <v-sheet rounded variant="tonal" color="primary" class="pa-2">
                <div class="text-caption">EMI</div>
                <div class="font-weight-bold">{{ money(detail.emi) }}</div>
              </v-sheet>
            </v-col>
            <v-col cols="6">
              <v-sheet rounded variant="tonal" color="error" class="pa-2">
                <div class="text-caption">Outstanding</div>
                <div class="font-weight-bold">{{ money(detail.outstanding) }}</div>
              </v-sheet>
            </v-col>
          </v-row>
          <div class="text-caption text-medium-emphasis mt-2">
            {{ money(detail.principal) }} at {{ detail.annual_interest_rate }}% for
            {{ detail.duration_months }} months from {{ detail.start_date }} (first
            EMI) · {{ detail.emis_due }} EMI{{ detail.emis_due === 1 ? '' : 's' }}
            due · total {{ money(detail.total_payable) }} · paid
            {{ money(detail.amount_paid) }}
            <v-chip v-if="detail.emi_edited" size="x-small" color="warning" variant="tonal">
              EMI edited
            </v-chip>
          </div>

          <v-btn
            color="success"
            block
            prepend-icon="mdi-cash-plus"
            class="mt-3"
            @click="openPay"
          >
            Record payment
          </v-btn>

          <div class="text-subtitle-2 mt-3 mb-1">Payment history</div>
          <v-table density="compact">
            <tbody>
              <tr v-for="p in payments" :key="p.id">
                <td>{{ p.date }}</td>
                <td>{{ p.note || '—' }}</td>
                <td class="text-right">{{ money(p.amount) }}</td>
              </tr>
              <tr v-if="!payments.length">
                <td colspan="3" class="text-medium-emphasis">
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

    <!-- Record payment against the open loan. -->
    <v-dialog v-model="payDialog" max-width="400">
      <v-card>
        <v-card-title>Record payment</v-card-title>
        <v-card-text>
          <v-text-field
            v-model.number="payForm.amount"
            type="number"
            prefix="₹"
            label="Amount"
          />
          <v-text-field v-model="payForm.date" type="date" label="Date" />
          <v-text-field v-model="payForm.note" label="Note (e.g. EMI, NEFT)" />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="payDialog = false">Cancel</v-btn>
          <v-btn color="success" @click="savePayment">Save</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>


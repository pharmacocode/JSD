<script setup>
/**
 * Assets & Liabilities (user request): the balance sheet behind the Home
 * tiles — fixed assets at today's depreciated value, stock in hand at the
 * vendor price from the material master, lifetime profit; and every loan's
 * outstanding, with the option to pay an EMI or transfer profits into it.
 */
import { computed, onActivated, onMounted, ref } from 'vue'
import { api } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, monthLabel, today } from '@/utils/format'
import AssetDialog from '@/components/AssetDialog.vue'
import LoanDialog from '@/components/LoanDialog.vue'
import EmptyState from '@/components/EmptyState.vue'

const ui = useUiStore()
const loading = ref(true)
const error = ref('')
const data = ref({ assets: {}, liabilities: {} })

const assetDialog = ref(false)
const editingAsset = ref(null)
const loanDialog = ref(false)
const editingLoan = ref(null)

// Pay an EMI / transfer from profits.
const payTarget = ref(null)
const payForm = ref({ amount: null, date: today(), note: 'EMI' })
const paySaving = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.get('/networth/')
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
onActivated(load)

const nw = computed(() => ({
  assets: data.value.assets || {},
  liabilities: data.value.liabilities || {},
  net: Number(data.value.net || 0),
}))

const tiles = computed(() => [
  {
    key: 'assets',
    label: 'Assets',
    value: money(nw.value.assets.total),
    color: 'success',
    icon: 'mdi-bank',
  },
  {
    key: 'liabilities',
    label: 'Liabilities',
    value: money(nw.value.liabilities.total),
    color: 'error',
    icon: 'mdi-hand-coin-outline',
  },
  {
    key: 'net',
    label: 'Net value',
    value: money(nw.value.net),
    color: nw.value.net < 0 ? 'error' : 'primary',
    icon: 'mdi-scale-balance',
  },
])

const profit = computed(() => nw.value.assets.profit || {})

function assetSubtitle(a) {
  if (a.kind !== 'DEPRECIATING')
    return `Non-depreciating · value never changes`
  return `Bought ${monthLabel(a.purchase_month)} · ${a.lifetime_years} yr life · scrap ${money(a.end_of_life_value)}`
}

// --- Add / edit / delete --------------------------------------------------
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

function openCreateLoan() {
  editingLoan.value = null
  loanDialog.value = true
}

function openEditLoan(l) {
  editingLoan.value = l
  loanDialog.value = true
}

async function removeLoan(l) {
  if (!confirm(`Delete loan "${l.name}"? Its payments go with it.`)) return
  try {
    await api.del(`/loans/${l.id}/`)
    ui.notify('Loan deleted')
    await load()
  } catch (e) {
    error.value = e.message
  }
}

// --- EMI payments / transfers from profits --------------------------------
function openPay(loan, note) {
  payTarget.value = loan
  payForm.value = { amount: Number(loan.emi), date: today(), note }
}

async function savePayment() {
  error.value = ''
  if (!Number(payForm.value.amount) || Number(payForm.value.amount) <= 0) {
    error.value = 'Enter a positive amount.'
    return
  }
  paySaving.value = true
  try {
    await api.post(`/loans/${payTarget.value.id}/payments/`, {
      amount: String(payForm.value.amount),
      date: payForm.value.date,
      note: payForm.value.note,
    })
    ui.notify('Payment recorded — outstanding reduced')
    payTarget.value = null
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    paySaving.value = false
  }
}
</script>
<template>
  <div>
    <v-alert v-if="error" type="error" density="compact" class="mb-4">
      {{ error }} — check the API connection.
    </v-alert>

    <!-- The three headline figures (the same ones the Home tiles show). -->
    <v-row dense class="mb-4">
      <v-col v-for="t in tiles" :key="t.key" cols="4">
        <v-card :color="t.color" variant="tonal" height="100%">
          <v-card-text class="text-center py-3">
            <v-icon :color="t.color" size="20">{{ t.icon }}</v-icon>
            <div class="text-caption font-weight-bold text-uppercase">{{ t.label }}</div>
            <div class="text-subtitle-2 font-weight-bold">{{ t.value }}</div>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <!-- ASSETS ---------------------------------------------------------------- -->
    <v-card class="mb-4" :loading="loading">
      <v-card-title class="d-flex align-center text-subtitle-1 font-weight-bold">
        <v-icon start color="success">mdi-bank</v-icon>
        Assets
        <v-spacer />
        <strong>{{ money(nw.assets.total) }}</strong>
      </v-card-title>
      <v-divider />
      <v-card-text>
        <div class="d-flex align-center mb-1">
          <div class="text-caption font-weight-bold text-uppercase text-medium-emphasis">
            Fixed assets — depreciating
          </div>
          <v-spacer />
          <v-btn
            size="x-small"
            variant="tonal"
            color="primary"
            prepend-icon="mdi-plus"
            @click="openCreateAsset"
          >
            Add asset
          </v-btn>
        </div>
        <v-table v-if="nw.assets.depreciating?.length" density="compact">
          <tbody>
            <tr v-for="a in nw.assets.depreciating" :key="a.id">
              <td>
                {{ a.name }}
                <div class="text-caption text-medium-emphasis">{{ assetSubtitle(a) }}</div>
              </td>
              <td class="text-right font-weight-medium">{{ money(a.current_value) }}</td>
              <td style="width: 72px">
                <v-btn icon="mdi-pencil-outline" size="x-small" variant="text" @click="openEditAsset(a)" />
                <v-btn icon="mdi-delete-outline" size="x-small" variant="text" color="error" @click="removeAsset(a)" />
              </td>
            </tr>
          </tbody>
        </v-table>
        <p v-else class="text-body-2 text-medium-emphasis mb-2">
          No depreciating assets yet.
        </p>

        <div class="text-caption font-weight-bold text-uppercase text-medium-emphasis mt-3 mb-1">
          Non-depreciating (savings etc.)
        </div>
        <v-table v-if="nw.assets.non_depreciating?.length" density="compact">
          <tbody>
            <tr v-for="a in nw.assets.non_depreciating" :key="a.id">
              <td>
                {{ a.name }}
                <div class="text-caption text-medium-emphasis">{{ assetSubtitle(a) }}</div>
              </td>
              <td class="text-right font-weight-medium">{{ money(a.current_value) }}</td>
              <td style="width: 72px">
                <v-btn icon="mdi-pencil-outline" size="x-small" variant="text" @click="openEditAsset(a)" />
                <v-btn icon="mdi-delete-outline" size="x-small" variant="text" color="error" @click="removeAsset(a)" />
              </td>
            </tr>
          </tbody>
        </v-table>
        <p v-else class="text-body-2 text-medium-emphasis mb-2">
          None yet — add savings or deposits as non-depreciating assets.
        </p>

        <div class="text-caption font-weight-bold text-uppercase text-medium-emphasis mt-3 mb-1">
          Stock in hand
        </div>
        <v-table v-if="nw.assets.stock?.length" density="compact">
          <tbody>
            <tr v-for="s in nw.assets.stock" :key="s.material_id">
              <td>
                {{ s.material }}
                <div class="text-caption text-medium-emphasis">
                  {{ s.qty }} {{ s.unit }} × {{ money(s.price_per_unit) }}
                </div>
              </td>
              <td class="text-right font-weight-medium">{{ money(s.value) }}</td>
            </tr>
          </tbody>
        </v-table>
        <p v-else class="text-body-2 text-medium-emphasis mb-2">No stock in hand.</p>
        <div class="text-caption text-medium-emphasis">
          Valued at the price we pay the vendor (material master).
        </div>

        <div class="text-caption font-weight-bold text-uppercase text-medium-emphasis mt-3 mb-1">
          Profits (lifetime)
        </div>
        <v-table density="compact">
          <tbody>
            <tr>
              <td>Revenue — all deliveries</td>
              <td class="text-right">{{ money(profit.revenue) }}</td>
            </tr>
            <tr>
              <td>Direct cost (current FIFO + print)</td>
              <td class="text-right">− {{ money(profit.direct_cost) }}</td>
            </tr>
            <tr>
              <td>Overhead booked so far</td>
              <td class="text-right">− {{ money(profit.overhead) }}</td>
            </tr>
            <tr>
              <td class="font-weight-bold">Profit</td>
              <td
                class="text-right font-weight-bold"
                :class="Number(profit.net) < 0 ? 'text-error' : 'text-success'"
              >
                {{ money(profit.net) }}
              </td>
            </tr>
          </tbody>
        </v-table>
      </v-card-text>
    </v-card>

    <!-- LIABILITIES ---------------------------------------------------------- -->
    <v-card class="mb-4" :loading="loading">
      <v-card-title class="d-flex align-center text-subtitle-1 font-weight-bold">
        <v-icon start color="error">mdi-hand-coin-outline</v-icon>
        Liabilities
        <v-spacer />
        <strong class="text-error">{{ money(nw.liabilities.total) }}</strong>
      </v-card-title>
      <v-divider />
      <v-card-text>
        <div class="d-flex align-center mb-2">
          <div class="text-caption text-medium-emphasis">
            Every loan's outstanding — pay an EMI or transfer profits into it.
          </div>
          <v-spacer />
          <v-btn
            size="x-small"
            variant="tonal"
            color="primary"
            prepend-icon="mdi-plus"
            @click="openCreateLoan"
          >
            Add loan
          </v-btn>
        </div>
        <EmptyState
          v-if="!nw.liabilities.loans?.length"
          icon="mdi-bank-outline"
          text="No loans — the liabilities side is clear."
          action="Add loan"
          @action="openCreateLoan"
        />
        <v-list v-if="nw.liabilities.loans?.length" density="compact" lines="two">
          <v-list-item
            v-for="l in nw.liabilities.loans"
            :key="l.id"
            :title="l.name"
            :subtitle="`EMI ${money(l.emi)} · ${l.annual_interest_rate}% · ${l.duration_months} mo · ${l.emis_due} due · paid ${money(l.amount_paid)}`"
          >
            <template #prepend>
              <v-avatar color="error" variant="tonal" size="36">
                <v-icon>mdi-hand-coin-outline</v-icon>
              </v-avatar>
            </template>
            <template #append>
              <div class="d-flex flex-column align-end mr-1">
                <div class="font-weight-bold text-error">{{ money(l.outstanding) }}</div>
                <div class="text-caption text-medium-emphasis">outstanding</div>
                <v-chip v-if="l.emi_edited" size="x-small" color="warning" variant="tonal" class="mt-1">
                  EMI edited
                </v-chip>
              </div>
              <v-btn
                size="x-small"
                variant="tonal"
                color="success"
                class="mr-1"
                title="Pay an EMI"
                @click.stop="openPay(l, 'EMI')"
              >
                Pay EMI
              </v-btn>
              <v-btn
                size="x-small"
                variant="tonal"
                color="info"
                class="mr-1"
                title="Transfer from profits to this loan's EMIs"
                @click.stop="openPay(l, 'Transferred from profits')"
              >
                From profits
              </v-btn>
              <v-btn
                icon="mdi-pencil-outline"
                size="x-small"
                variant="text"
                @click.stop="openEditLoan(l)"
              />
              <v-btn
                icon="mdi-delete-outline"
                size="x-small"
                variant="text"
                color="error"
                @click.stop="removeLoan(l)"
              />
            </template>
          </v-list-item>
        </v-list>
      </v-card-text>
    </v-card>

    <AssetDialog v-model="assetDialog" :asset="editingAsset" @saved="load" />
    <LoanDialog v-model="loanDialog" :loan="editingLoan" @saved="load" />

    <!-- Pay an EMI / transfer from profits (same movement, different note). -->
    <v-dialog :model-value="!!payTarget" max-width="400" @update:model-value="(v) => !v && (payTarget = null)">
      <v-card v-if="payTarget">
        <v-card-title>
          {{ payForm.note === 'Transferred from profits' ? 'Transfer from profits' : 'Pay EMI' }}
        </v-card-title>
        <v-card-text>
          <div class="text-body-2 text-medium-emphasis mb-2">
            {{ payTarget.name }} · outstanding {{ money(payTarget.outstanding) }} ·
            EMI {{ money(payTarget.emi) }}
          </div>
          <v-text-field
            v-model.number="payForm.amount"
            type="number"
            prefix="₹"
            label="Amount"
          />
          <v-text-field v-model="payForm.date" type="date" label="Date" />
          <v-text-field v-model="payForm.note" label="Note" />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="payTarget = null">Cancel</v-btn>
          <v-btn color="success" :loading="paySaving" @click="savePayment">Save</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>


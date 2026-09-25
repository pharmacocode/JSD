<script setup>
/**
 * Client detail (spec 4.5): contact + Google Maps link-out, SKU prices,
 * large pending amount, Record Payment, Ledger/Deliveries tabs.
 */
import { ref, onMounted, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, listify } from '@/api'
import { money, today } from '@/utils/format'
import { useUiStore } from '@/stores/ui'
import AuditBadge from '@/components/AuditBadge.vue'
import BalanceMarkerDialog from '@/components/BalanceMarkerDialog.vue'
import EmptyState from '@/components/EmptyState.vue'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()
const id = route.params.id

const client = ref(null)
const ledger = ref({ entries: [], pending_amount: '0' })
const deliveries = ref([])
const tab = ref('ledger')
const loading = ref(true)

const payDialog = ref(false)
// Pending amount + the date it refers to (editable at any time, user request).
const markerDialog = ref(false)
const payAmount = ref(null)
const payDate = ref(today())
const payNote = ref('')
const saving = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  try {
    const [c, l, d] = await Promise.all([
      api.get(`/clients/${id}/`),
      api.get(`/clients/${id}/ledger/`),
      api.get(`/clients/${id}/deliveries/`),
    ])
    client.value = c
    ledger.value = l
    deliveries.value = listify(d)
  } finally {
    loading.value = false
  }
}
onMounted(load)

const pending = computed(() => ledger.value.pending_amount)
const markerActive = computed(() => !!client.value?.pending_as_of_date)

const TYPE_COLORS = {
  DELIVERY: 'primary',
  PAYMENT: 'success',
  ADJUSTMENT: 'warning',
  OPENING_BALANCE: 'grey',
}

async function recordPayment() {
  error.value = ''
  if (!payAmount.value || Number(payAmount.value) <= 0) {
    error.value = 'Enter a positive amount.'
    return
  }
  saving.value = true
  try {
    await api.post(`/clients/${id}/payment/`, {
      amount: String(payAmount.value),
      date: payDate.value,
      note: payNote.value,
    })
    ui.notify(`Payment of ${money(payAmount.value)} recorded`)
    payDialog.value = false
    payAmount.value = null
    payNote.value = ''
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

async function deleteEntry(entry) {
  if (!confirm('Soft-delete this ledger entry? It stays in the audit trail.')) return
  await api.del(`/ledger/${entry.id}/`)
  await load()
}
</script>

<template>
  <div v-if="client">
    <!-- Header card -->
    <v-card class="mb-3">
      <v-card-title class="d-flex align-center">
        {{ client.name }}
        <v-spacer />
        <v-btn
          :to="`/clients/${id}/edit`"
          icon="mdi-pencil"
          size="small"
          variant="text"
        />
      </v-card-title>
      <v-card-text>
        <!-- Link-out only, no embedded map (spec 9) -->
        <v-btn
          v-if="client.google_maps_url"
          :href="client.google_maps_url"
          target="_blank"
          rel="noopener"
          color="secondary"
          variant="tonal"
          size="small"
          prepend-icon="mdi-map-marker"
          class="mr-2 mb-2"
        >
          Open in Google Maps
        </v-btn>
        <v-btn
          v-if="client.contact_number"
          :href="`tel:${client.contact_number}`"
          color="primary"
          variant="tonal"
          size="small"
          prepend-icon="mdi-phone"
          class="mb-2"
        >
          {{ client.contact_number }}
        </v-btn>

        <!-- Large prominent pending amount (spec 4.5). The figure is editable
             at any time together with the date it refers to; ledger entries
             after that day are added on top automatically. -->
        <v-sheet
          :color="Number(pending) > 0 ? 'error' : 'success'"
          rounded="lg"
          class="mt-3 pa-4 text-center"
        >
          <div class="text-caption" style="color: #fff">Pending amount</div>
          <div class="text-h4 font-weight-bold" style="color: #fff">
            {{ money(pending) }}
          </div>
          <div class="text-caption mt-1" style="color: #fff">
            {{
              markerActive
                ? `as of ${client.pending_as_of_date} · later transactions added`
                : 'live sum of the ledger'
            }}
          </div>
          <v-btn
            size="small"
            variant="outlined"
            color="white"
            class="mt-2"
            prepend-icon="mdi-calendar-edit"
            @click="markerDialog = true"
          >
            Edit pending amount
          </v-btn>
        </v-sheet>

        <v-btn
          color="success"
          block
          size="large"
          class="mt-3"
          prepend-icon="mdi-cash-plus"
          @click="payDialog = true"
        >
          Record Payment
        </v-btn>
      </v-card-text>
    </v-card>

    <!-- Preferred SKUs + prices -->
    <v-card class="mb-3">
      <v-card-title class="text-subtitle-1">SKUs & prices</v-card-title>
      <v-divider />
      <v-table density="compact">
        <tbody>
          <tr v-for="p in client.sku_prices" :key="p.id">
            <td>{{ p.sku_description }}</td>
            <td class="text-right font-weight-bold">
              {{ money(p.selling_price_per_case) }} / case
            </td>
          </tr>
          <tr v-if="!client.sku_prices?.length">
            <td colspan="2" class="text-medium-contrast">
              No SKUs configured — edit the client to add them.
            </td>
          </tr>
        </tbody>
      </v-table>
    </v-card>

    <!-- Ledger / Deliveries tabs (spec 4.5) -->
    <v-tabs v-model="tab" color="primary" class="mb-2">
      <v-tab value="ledger">Ledger</v-tab>
      <v-tab value="deliveries">Deliveries</v-tab>
    </v-tabs>

    <v-tabs-window v-model="tab">
      <v-tabs-window-item value="ledger">
        <v-card :loading="loading">
          <v-alert
            v-if="ledger.marker_active"
            type="info"
            variant="tonal"
            density="compact"
            class="ma-2"
          >
            {{ money(ledger.pending_as_of_amount) }} as of
            {{ ledger.pending_as_of_date }} — the
            {{ ledger.superseded_count }} entry(s) dated on/before that day
            ({{ money(ledger.superseded_total) }}) are already inside it, so only
            later entries are added on top.
          </v-alert>
          <EmptyState
            v-if="!ledger.entries.length"
            icon="mdi-book-open"
            text="No ledger entries yet — deliveries and payments appear here."
          />
          <v-list v-else density="compact">
            <v-list-item
              v-for="e in ledger.entries"
              :key="e.id"
              :title="e.note || e.entry_type"
              :subtitle="`${e.date}${e.delivery_ref ? ' · ' + e.delivery_ref : ''}`"
            >
              <template #prepend>
                <v-avatar :color="TYPE_COLORS[e.entry_type]" size="28" variant="tonal">
                  <v-icon size="small">
                    {{
                      e.entry_type === 'PAYMENT'
                        ? 'mdi-cash-minus'
                        : e.entry_type === 'DELIVERY'
                          ? 'mdi-truck'
                          : 'mdi-book'
                    }}
                  </v-icon>
                </v-avatar>
              </template>
              <template #append>
                <div class="text-right">
                  <div
                    :class="Number(e.amount) >= 0 ? 'text-error' : 'text-success'"
                    class="font-weight-bold"
                  >
                    {{ Number(e.amount) >= 0 ? '+' : '' }}{{ money(e.amount) }}
                  </div>
                  <div class="text-caption text-medium-contrast">
                    {{
                      e.running_balance
                        ? `bal ${money(e.running_balance)}`
                        : 'inside the as-of figure'
                    }}
                  </div>
                  <div class="d-flex justify-end align-center ga-1 mt-1">
                    <v-chip
                      v-if="e.is_superseded"
                      size="x-small"
                      variant="tonal"
                      color="grey"
                    >
                      as-of
                    </v-chip>
                    <AuditBadge :record="e" />
                    <v-btn
                      icon="mdi-delete-outline"
                      size="x-small"
                      variant="text"
                      color="error"
                      @click="deleteEntry(e)"
                    />
                  </div>
                </div>
              </template>
            </v-list-item>
          </v-list>
        </v-card>
      </v-tabs-window-item>

      <v-tabs-window-item value="deliveries">
        <v-card :loading="loading">
          <EmptyState
            v-if="!deliveries.length"
            icon="mdi-truck"
            text="No deliveries yet — tap + on Home to add one."
            action="Add delivery"
            @action="router.push('/delivery/new')"
          />
          <v-list v-else density="compact">
            <v-list-item
              v-for="d in deliveries"
              :key="d.id"
              :title="`${d.sku_description} × ${d.qty_cases} cases`"
              :subtitle="`${d.date} · COGS ${money(d.cogs_per_case_current ?? d.cogs_per_case_snapshot)}/case (base ${money(d.base_cogs_per_case ?? 0)} + live OH ${money(d.overhead_per_case_current ?? 0)})`"
            >
              <template #append>
                <div class="text-right">
                  <div class="font-weight-bold">{{ money(d.total_amount) }}</div>
                  <AuditBadge :record="d" />
                  <v-chip
                    v-if="d.stock_shortfall_flag"
                    color="warning"
                    size="x-small"
                    variant="tonal"
                    class="mt-1"
                  >
                    shortfall
                  </v-chip>
                </div>
              </template>
            </v-list-item>
          </v-list>
        </v-card>
      </v-tabs-window-item>
    </v-tabs-window>

    <!-- Record Payment dialog -->
    <v-dialog v-model="payDialog" max-width="420">
      <v-card>
        <v-card-title>Record Payment</v-card-title>
        <v-card-text>
          <v-alert v-if="error" type="error" density="compact" class="mb-2">
            {{ error }}
          </v-alert>
          <v-text-field
            v-model.number="payAmount"
            type="number"
            min="0"
            prefix="₹"
            label="Amount received"
          />
          <v-text-field v-model="payDate" type="date" label="Date" />
          <v-text-field v-model="payNote" label="Note (e.g. Cash payment received)" />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="payDialog = false">Cancel</v-btn>
          <v-btn color="success" :loading="saving" @click="recordPayment">
            Save
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Pending amount as of a date (editable at any time) -->
    <BalanceMarkerDialog
      v-model="markerDialog"
      title="Pending amount as of a date"
      amount-label="Pending amount on that date"
      total-label="Pending amount now"
      :endpoint="`/clients/${id}/pending/`"
      :amount="client.pending_as_of_amount"
      :date="client.pending_as_of_date"
      :active="markerActive"
      @saved="load"
    />
  </div>

  <v-progress-circular v-else-if="loading" indeterminate class="d-block mx-auto mt-8" />
</template>


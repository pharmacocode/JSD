<script setup>
/**
 * Add Delivery (user request) - the Ready to Deliver queue.
 *
 * This screen used to be a free-form client + SKU form, so anything could be
 * sent out whether or not it had ever been ordered. It now lists ONLY the
 * orders that were walked through Enter Order -> Ready to Deliver: one tap on
 * Deliver opens a confirmation (client, lines, cases, value, editable date,
 * note and an OPTIONAL payment) and confirming sends the whole order in ONE
 * request.
 *
 * The confirmation's lines can also be trimmed (user request: partial
 * delivery): untick a line or dial a quantity down and only those cases leave,
 * with the rest staying in this queue — the same order, now tracking what is
 * left. Nothing is duplicated either way: a line sent in part is split, a line
 * sent whole keeps its identity.
 *
 * The API enforces the same rule: POST /orders/<id>/deliver/ refuses an order
 * that is still PENDING and refuses one that is already fully delivered, so
 * this screen is the single sanctioned path a delivery can take (StockDelivery
 * admin add is disabled for the same reason).
 *
 * One order = one atomic operation: a StockDelivery per undelivered line plus
 * the ledger entries, with the optional payment written in the same
 * transaction. A stock shortfall (409) opens the Proceed anyway / Cancel banner
 * instead of leaving the user with a dead button.
 */
import { ref, computed, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useOrdersStore } from '@/stores/orders'
import { useRefDataStore } from '@/stores/refdata'
import { useUiStore } from '@/stores/ui'
import EmptyState from '@/components/EmptyState.vue'
import { money, num, today, dateLong } from '@/utils/format'

const router = useRouter()
const orders = useOrdersStore()
const refdata = useRefDataStore()
const ui = useUiStore()

const busy = ref(false)
const error = ref('')
// 409 shortages -> inline "Proceed anyway" banner (same pattern as StatusUpdate).
const shortfall = ref(null)
// Receipt for the order just delivered, cleared when the next one is started.
const result = ref(null)

// Confirmation dialog. `selected` non-null == dialog open.
const selected = ref(null)
const date = ref(today())
const note = ref('')
const payNow = ref(false)
const payAmount = ref('')
const payDate = ref(today())
const payNote = ref('')

const ready = computed(() => orders.ready)
// Only the very first load may blank the list; a refresh keeps the rows on
// screen under the spinner.
const loading = computed(() => orders.loading && !orders.ready.length)
const problem = computed(() => orders.error || error.value)

/** Lines still to go out - an already-delivered line must not be sent twice. */
function pendingLines(order) {
  return (order?.items || []).filter((i) => !i.delivery)
}

function pendingQty(order) {
  return pendingLines(order).reduce((sum, l) => sum + Number(l.qty_cases || 0), 0)
}

function pendingValue(order) {
  return pendingLines(order).reduce(
    (sum, l) => sum + Number(l.line_amount || 0),
    0
  )
}

// What the confirmation dialog shows - and exactly what the server will send.
const lines = computed(() => pendingLines(selected.value))
const alreadyDelivered = computed(
  () => (selected.value?.items?.length || 0) - lines.value.length
)

// ---------------------------------------------------------------------------
// How much of each line goes out (user request: partial delivery)
// ---------------------------------------------------------------------------
// id -> { on, take }. Every line starts ticked at its full quantity, so the
// usual delivery is still "tap Deliver, tap Confirm"; untick a line or dial a
// quantity down and only those cases leave the warehouse, with the rest of the
// order staying in this queue until it is sent too.
const picks = ref({})

function resetPicks(order) {
  const next = {}
  for (const l of pendingLines(order)) {
    next[l.id] = { on: true, take: Number(l.qty_cases) }
  }
  picks.value = next
}

/** The cases actually going out in this delivery. */
const sending = computed(() =>
  lines.value.filter(
    (l) => picks.value[l.id]?.on && Number(picks.value[l.id].take) > 0
  )
)

const runCases = computed(() =>
  sending.value.reduce((sum, l) => sum + Number(picks.value[l.id].take), 0)
)

const runValue = computed(() =>
  sending.value.reduce(
    (sum, l) =>
      sum + Number(picks.value[l.id].take) * Number(l.selling_price_per_case),
    0
  )
)

/** Ticked quantities that are empty, zero or more than the line holds. */
const badLines = computed(() =>
  lines.value.filter((l) => {
    const p = picks.value[l.id]
    if (!p || !p.on) return false
    const take = Number(p.take)
    return !(take > 0) || take > Number(l.qty_cases)
  })
)

/** Some cases of this order stay behind: this is a partial delivery. */
const partial = computed(
  () =>
    sending.value.length !== lines.value.length ||
    sending.value.some(
      (l) => Number(picks.value[l.id].take) !== Number(l.qty_cases)
    )
)

/** The per-line selection the API takes: [{item, qty_cases}]. */
function selection() {
  return sending.value.map((l) => ({
    item: l.id,
    qty_cases: String(Number(picks.value[l.id].take)),
  }))
}

// v-model for the dialog: backdrop, Esc and Cancel all run the same reset.
const confirmOpen = computed({
  get: () => !!selected.value,
  set: (open) => {
    if (!open) closeConfirm()
  },
})

const canConfirm = computed(() => {
  if (busy.value || !selected.value || !lines.value.length) return false
  // At least one line has to be going out, and every ticked quantity has to be
  // a real number of cases (user request: partial delivery).
  if (!sending.value.length || badLines.value.length) return false
  // The payment is optional, but once the switch is on the amount must be
  // positive - otherwise the money would look noted when nothing was written.
  if (payNow.value && !(Number(payAmount.value) > 0)) return false
  return true
})

// Money usually changes hands on the delivery date, so the payment date follows
// it until the user deliberately picks another day.
watch(date, (value, previous) => {
  if (payDate.value === previous) payDate.value = value
})

// Flipping the switch on offers the full order value, which is the usual case.
watch(payNow, (on) => {
  if (!on) return
  payDate.value = date.value
  if (!(Number(payAmount.value) > 0)) payAmount.value = String(runValue.value)
})

async function load() {
  error.value = ''
  await orders.refresh(true)
}

function openConfirm(order) {
  selected.value = order
  error.value = ''
  shortfall.value = null
  date.value = today()
  note.value = ''
  payNow.value = false
  payAmount.value = ''
  payDate.value = today()
  payNote.value = ''
  // Every line ticked at full quantity: the whole order, unless touched.
  resetPicks(order)
}

function closeConfirm() {
  selected.value = null
  shortfall.value = null
  picks.value = {}
}

function deliverAnother() {
  result.value = null
  error.value = ''
}

async function submit(force = false) {
  const order = selected.value
  if (!order || busy.value) return
  busy.value = true
  error.value = ''
  shortfall.value = null
  try {
    const payment =
      payNow.value && Number(payAmount.value) > 0
        ? {
            amount: String(payAmount.value),
            date: payDate.value,
            note: payNote.value,
          }
        : null
    result.value = await orders.deliver(order.id, {
      date: date.value,
      note: note.value,
      force,
      payment,
      // The whole order keeps the original request shape (no `items` at all);
      // only a partial delivery needs the per-line selection.
      items: partial.value ? selection() : null,
    })
    // Stock moved and the client's pending balance changed: drop the cached
    // reference lists so the next screen reads fresh figures.
    refdata.invalidate('materials', 'clients')
    ui.notify(
      result.value.partial
        ? `Order #${order.id} part delivered — ${num(result.value.order.undelivered_qty)} cases still to go`
        : `Order #${order.id} delivered`
    )
    selected.value = null
  } catch (e) {
    if (e.status === 409 && e.data?.shortages) {
      shortfall.value = e.data.shortages
    } else {
      error.value = e.message || 'Could not deliver this order'
    }
  } finally {
    busy.value = false
  }
}

// Entering the screen is the load; the header carries a manual refresh too.
onMounted(load)
</script>

<template>
  <div>
    <div class="mb-4">
      <div class="text-subtitle-1 font-weight-bold">Add delivery</div>
      <div class="text-caption text-medium-emphasis">
        Orders that reached Ready to Deliver. One tap delivers the whole order,
        so nothing can go out that was never ordered — or trim the cases on the
        confirmation to send part now and leave the rest queued.
      </div>
    </div>

    <v-alert v-if="problem" type="error" density="compact" class="mb-3">
      {{ problem }}
    </v-alert>

    <!-- Receipt for the order just delivered: what went out, what it was
         worth, the payment if one was handed over, and the client's pending
         balance afterwards (all figures come back from the server). -->
    <v-card v-if="result" class="mb-3">
      <v-card-item class="py-3">
        <div class="d-flex align-center ga-3">
          <v-icon color="success">mdi-check-circle-outline</v-icon>
          <div>
            <div class="text-body-2 font-weight-bold">
              Order #{{ result.order.id }}
              {{ result.partial ? "part delivered" : "delivered" }}
            </div>
            <div class="text-caption text-medium-emphasis">
              {{ result.order.client_name }} -
              {{ dateLong(result.order.order_date) }}
            </div>
            <!-- A partial delivery leaves the order in the queue for the rest
                 (user request), so say so here rather than let it look done. -->
            <div v-if="result.partial" class="text-caption text-info">
              {{ num(result.order.undelivered_qty) }} cases still to go — still
              in Ready to Deliver.
            </div>
          </div>
        </div>
      </v-card-item>
      <v-divider />
      <v-list density="compact">
        <v-list-item
          v-for="row in result.deliveries"
          :key="row.id"
          :title="row.sku"
          :subtitle="`${num(row.qty_cases)} cases at ${money(row.selling_price_per_case)}`"
        >
          <template #append>
            <div class="text-right">
              <div class="text-body-2 font-weight-bold">
                {{ money(row.amount) }}
              </div>
              <v-chip
                v-if="row.stock_shortfall_flag"
                size="x-small"
                color="warning"
                variant="flat"
              >
                stock short
              </v-chip>
            </div>
          </template>
        </v-list-item>
        <v-divider class="my-1" />
        <v-list-item :title="`${num(result.total_cases)} cases delivered`">
          <template #append>
            <strong>{{ money(result.total_amount) }}</strong>
          </template>
        </v-list-item>
        <v-list-item
          v-if="result.payment"
          title="Payment recorded with the delivery"
          :subtitle="`${dateLong(result.payment.date)}${result.payment.note ? ' - ' + result.payment.note : ''}`"
        >
          <template #append>
            <strong class="text-success">
              {{ money(result.payment.amount) }}
            </strong>
          </template>
        </v-list-item>
        <v-list-item
          :title="
            result.payment
              ? 'Client pending balance (after this payment)'
              : 'Client pending balance'
          "
        >
          <template #append>
            <strong class="text-error">
              {{ money(result.client_pending_amount) }}
            </strong>
          </template>
        </v-list-item>
      </v-list>
      <v-divider />
      <v-card-actions>
        <v-btn
          color="primary"
          variant="flat"
          prepend-icon="mdi-truck-check-outline"
          @click="deliverAnother"
        >
          Deliver another order
        </v-btn>
        <v-spacer />
        <v-btn
          variant="text"
          @click="router.push(`/clients/${result.order.client}`)"
        >
          Client ledger
        </v-btn>
        <v-btn variant="text" @click="router.push('/')">Home</v-btn>
      </v-card-actions>
    </v-card>

    <!-- The queue itself: exactly the orders the summary lists as Ready to
         Deliver, showing the quantity and value still to go out (a line that
         was already delivered is neither counted nor sent again). -->
    <v-card v-else :loading="loading">
      <v-card-item class="py-3">
        <div class="d-flex align-center ga-3">
          <v-chip
            :color="ready.length ? 'success' : 'grey'"
            variant="flat"
            size="small"
            class="font-weight-bold"
          >
            {{ ready.length }}
          </v-chip>
          <span class="text-uppercase font-weight-bold text-medium-emphasis">
            Ready to Deliver
          </span>
          <v-spacer />
          <v-btn
            variant="text"
            size="small"
            color="primary"
            prepend-icon="mdi-refresh"
            :loading="loading"
            @click="load"
          >
            Refresh
          </v-btn>
        </div>
      </v-card-item>
      <v-divider />
      <v-card-text class="pt-3">
        <EmptyState
          v-if="!ready.length"
          icon="mdi-truck-outline"
          text="Nothing is ready to deliver yet. Move an order to Ready to Deliver first."
          action="Open status update"
          @action="router.push('/statusupdate')"
        />
        <div v-else>
          <v-card v-for="o in ready" :key="o.id" variant="outlined" class="mb-3">
            <v-card-item class="py-2">
              <div class="d-flex align-center ga-2">
                <div>
                  <div class="text-body-2 font-weight-bold">
                    #{{ o.id }} - {{ o.client_name }}
                  </div>
                  <div class="text-caption text-medium-emphasis">
                    ordered {{ dateLong(o.order_date) }} -
                    {{ num(pendingQty(o)) }} cases -
                    {{ money(pendingValue(o)) }}
                  </div>
                </div>
                <v-chip
                  v-if="pendingLines(o).length < (o.items || []).length"
                  size="x-small"
                  color="info"
                  variant="flat"
                >
                  part delivered
                </v-chip>
                <v-spacer />
                <v-btn
                  color="primary"
                  variant="flat"
                  size="small"
                  prepend-icon="mdi-truck-check-outline"
                  :disabled="busy"
                  @click="openConfirm(o)"
                >
                  Deliver
                </v-btn>
              </div>
            </v-card-item>
          </v-card>
        </div>
      </v-card-text>
    </v-card>

    <!-- Confirmation: the one place a delivery is created from, so it shows
         exactly what the server will send (the remaining lines) and lets the
         optional payment be noted in the same request. -->
    <v-dialog v-model="confirmOpen" max-width="560">
      <v-card v-if="selected">
        <v-card-item class="py-3">
          <div class="text-body-1 font-weight-bold">
            {{
              partial
                ? `Deliver part of order #${selected.id}?`
                : `Deliver order #${selected.id}?`
            }}
          </div>
          <div class="text-caption text-medium-emphasis">
            {{ selected.client_name }} - {{ num(runCases) }} cases -
            {{ money(runValue) }}
          </div>
        </v-card-item>
        <v-divider />
        <v-card-text>
          <div class="d-flex align-center mb-1">
            <span class="text-uppercase text-caption text-medium-emphasis">
              Cases going out now
            </span>
            <v-spacer />
            <v-btn
              variant="text"
              size="small"
              :disabled="busy"
              @click="resetPicks(selected)"
            >
              All
            </v-btn>
          </div>
          <v-list density="compact" class="py-0">
            <v-list-item v-for="l in lines" :key="l.id" class="px-0">
              <template #prepend>
                <v-checkbox-btn
                  v-model="picks[l.id].on"
                  density="compact"
                  :disabled="busy"
                  hide-details
                />
              </template>
              <v-list-item-title class="text-body-2 font-weight-medium">
                {{ l.sku_description }}
              </v-list-item-title>
              <v-list-item-subtitle class="text-caption">
                {{ num(l.qty_cases) }} cases at
                {{ money(l.selling_price_per_case) }}
              </v-list-item-subtitle>
              <template #append>
                <div class="d-flex align-center ga-2">
                  <!-- Dial the quantity down to send part of a line; the rest
                       stays on the order (user request). -->
                  <v-text-field
                    v-model.number="picks[l.id].take"
                    type="number"
                    min="0"
                    :max="l.qty_cases"
                    density="compact"
                    variant="outlined"
                    hide-details
                    single-line
                    style="max-width: 96px"
                    :disabled="busy || !picks[l.id].on"
                    :error="badLines.includes(l)"
                  />
                  <span class="text-caption text-medium-emphasis text-no-wrap">
                    of {{ num(l.qty_cases) }}
                  </span>
                </div>
              </template>
            </v-list-item>
          </v-list>

          <v-alert
            v-if="badLines.length"
            type="warning"
            variant="tonal"
            density="compact"
            class="mt-3"
          >
            A ticked line needs a quantity between 1 and the cases it holds
            ({{ badLines.length }} line(s) to fix).
          </v-alert>

          <v-alert
            v-else-if="partial"
            type="info"
            variant="tonal"
            density="compact"
            class="mt-3"
          >
            Part delivery: the cases left behind stay on this order in Ready to
            Deliver, and the value and ledger only move by the
            {{ num(runCases) }} cases going out.
          </v-alert>

          <v-alert
            v-if="alreadyDelivered"
            type="info"
            variant="tonal"
            density="compact"
            class="my-3"
          >
            {{ alreadyDelivered }} line(s) of this order were already delivered.
            Only the lines above go out now.
          </v-alert>

          <v-text-field
            v-model="date"
            label="Delivery date"
            type="date"
            variant="outlined"
            density="compact"
            class="mb-1"
          />
          <v-text-field
            v-model="note"
            label="Note (optional)"
            variant="outlined"
            density="compact"
          />

          <!-- Payment is optional and off by default: leave the switch alone
               and the order goes out on credit, exactly as before. Turn it on
               and the money is written in the same transaction as the stock. -->
          <v-switch
            v-model="payNow"
            color="primary"
            density="compact"
            hide-details
            label="Payment received now"
            class="mt-2"
          />
          <div v-if="payNow" class="mt-3">
            <v-text-field
              v-model="payAmount"
              label="Amount (INR)"
              type="number"
              min="0"
              step="0.01"
              variant="outlined"
              density="compact"
              class="mb-1"
            />
            <v-text-field
              v-model="payDate"
              label="Payment date"
              type="date"
              variant="outlined"
              density="compact"
              class="mb-1"
            />
            <v-text-field
              v-model="payNote"
              label="Payment note (optional)"
              placeholder="UPI, cash, cheque no."
              variant="outlined"
              density="compact"
            />
          </div>

          <!-- A shortfall is not fatal: the banner turns Confirm into Proceed
               anyway, which resends with force=true - the same escape hatch
               Enter Order and Status Update offer. -->
          <v-alert
            v-if="shortfall"
            type="warning"
            variant="tonal"
            density="compact"
            class="mt-3"
          >
            Not enough stock for this order:
            <div v-for="s in shortfall" :key="s.material_id">
              {{ s.material }} short by {{ num(s.short_by) }} {{ s.unit }}
            </div>
          </v-alert>
        </v-card-text>
        <v-divider />
        <v-card-actions>
          <v-btn variant="text" @click="closeConfirm">Cancel</v-btn>
          <v-spacer />
          <v-btn
            v-if="shortfall"
            color="warning"
            variant="flat"
            :loading="busy"
            @click="submit(true)"
          >
            Proceed anyway
          </v-btn>
          <v-btn
            v-else
            color="primary"
            variant="flat"
            prepend-icon="mdi-truck-check-outline"
            :disabled="!canConfirm"
            :loading="busy"
            @click="submit(false)"
          >
            {{ partial ? "Deliver part of this order" : "Confirm delivery" }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

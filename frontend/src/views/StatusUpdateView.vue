<script setup>
/**
 * Order status board (user request).
 *
 * A deliberately plain screen with ONE job: move orders between Pending and
 * Ready to Deliver. It has no app bar, drawer or bottom nav (App.vue hides
 * every one of them for `meta.bare` routes), so there is nothing here that
 * leads back to Home — the only way off it is the browser.
 *
 * "Mark complete" is what moves an order's material out of Stock in Hand into
 * Ready to Deliver, so this screen is where the warehouse-side commitment is
 * actually made. A 409 (not enough free stock) is surfaced as an inline
 * warning with the shortfall detail rather than a dead button.
 *
 * Both buttons open a picker (OrderLinesDialog) with every line pre-ticked, so
 * the whole-order move is still a confirm and a partial move is the same
 * picker with some quantities dialled down (user request). A partial move
 * splits the order in two on the server, which is why a move reloads the lists
 * instead of patching the row it started from. Orders that have sent nothing
 * can be deleted from here too.
 */
import { ref, computed, onMounted } from 'vue'
import { api } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, num } from '@/utils/format'
import OrderLinesDialog from '@/components/OrderLinesDialog.vue'
import ConfirmDialog from '@/components/ConfirmDialog.vue'

const ui = useUiStore()

const rows = ref([])
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    rows.value = await api.get('/orders/')
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

const pending = computed(() => rows.value.filter((o) => o.status === 'PENDING'))
const completed = computed(() => rows.value.filter((o) => o.status !== 'PENDING'))

/** True when some of the order's cases have already left the warehouse. */
function partDelivered(order) {
  return Number(order.undelivered_qty ?? order.total_qty) < Number(order.total_qty)
}

/**
 * The lines this board can still act on: anything not already delivered, so a
 * part-delivered order lists what is left rather than what has gone.
 */
function openLines(order) {
  const lines = (order.items || []).filter((i) => !i.delivery)
  return lines.length ? lines : order.items || []
}

/** The lines a partial move can take, with the quantity and price to show. */
function movableLines(order) {
  return openLines(order).map((i) => ({
    id: i.id,
    label: i.sku_description,
    detail: `${num(i.qty_cases)} cases at ${money(i.selling_price_per_case)} each`,
    qty: Number(i.qty_cases),
    price: Number(i.selling_price_per_case),
  }))
}

function skuSummary(order) {
  return openLines(order)
    .map((i) => `${i.sku_description} × ${num(i.qty_cases)}`)
    .join(' · ')
}

/**
 * Cases still owed lead the row, because that — not the original total — is
 * what this board can still move or deliver.
 */
function subtitle(order) {
  const owed = Number(order.undelivered_qty ?? order.total_qty)
  const total = Number(order.total_qty)
  const figure =
    owed < total ? `${num(owed)} of ${num(total)} cases left` : `${num(total)} cases`
  // "split from #12": this order was carved out of another one (partial move).
  const lineage = order.source_order ? ` · split from #${order.source_order}` : ''
  return `${figure} · ${skuSummary(order)}${lineage}`
}

// ---------------------------------------------------------------------------
// Moving cases, and deleting an order (user request)
// ---------------------------------------------------------------------------
// Both directions use the same dialog and the same two endpoints; partial
// moves split the order server-side, so a move always ends with a reload and
// the two lists are read back rather than patched in place.
const moveOrder = ref(null)
const moveMode = ref('ready')
const moveOpen = ref(false)
const moveBusy = ref(false)
const moveError = ref('')
const moveShortfall = ref(null)

const deleteOrder = ref(null)
const deleteOpen = ref(false)
const deleteBusy = ref(false)
const deleteError = ref('')

const moveToReady = computed(() => moveMode.value === 'ready')
const moveTitle = computed(() =>
  moveToReady.value ? 'Move to Ready to Deliver' : 'Move back to Pending'
)
const moveColor = computed(() => (moveToReady.value ? 'primary' : 'warning'))
const moveIcon = computed(() =>
  moveToReady.value ? 'mdi-truck-fast-outline' : 'mdi-undo'
)
const moveSubtitle = computed(() =>
  moveOrder.value
    ? `#${moveOrder.value.id} — ${moveOrder.value.client_name}: pick how much of each line crosses.`
    : ''
)
const moveConfirmLabel = computed(() =>
  moveShortfall.value
    ? 'Move anyway'
    : moveToReady.value
      ? 'Move to Ready to Deliver'
      : 'Move to Pending'
)

function openMove(order, mode) {
  moveOrder.value = order
  moveMode.value = mode
  moveError.value = ''
  moveShortfall.value = null
  moveOpen.value = true
}

async function doMove(selection) {
  const order = moveOrder.value
  if (!order || moveBusy.value) return
  moveBusy.value = true
  moveError.value = ''
  try {
    // A 409 does not close the dialog: the gap is shown and "Move anyway"
    // re-sends the same selection with force (the existing shortfall flow).
    const result = await api.post(
      `/orders/${order.id}/${moveToReady.value ? 'complete' : 'reopen'}/`,
      { items: selection, force: Boolean(moveShortfall.value) }
    )
    moveOpen.value = false
    await load()
    const where = moveToReady.value ? 'Ready to Deliver' : 'Pending'
    ui.notify(
      result?.split
        ? `Order #${order.id} split — #${result.split.id} moved to ${where}`
        : `Order #${order.id} moved to ${where}`
    )
  } catch (e) {
    if (e.data?.shortages?.length) {
      moveShortfall.value = e.data.shortages
      moveError.value =
        'Not enough free stock: ' +
        e.data.shortages
          .map(
            (g) =>
              `${g.material}: need ${num(g.required)}, only ${num(g.available)} free`
          )
          .join('; ')
    } else {
      moveError.value = e.message || 'Could not move these cases'
    }
  } finally {
    moveBusy.value = false
  }
}

function openDelete(order) {
  deleteOrder.value = order
  deleteError.value = ''
  deleteOpen.value = true
}

const deleteText = computed(() => {
  const o = deleteOrder.value
  if (!o) return ''
  const shipped = Number(o.total_qty) - Number(o.undelivered_qty ?? o.total_qty)
  return shipped > 0
    ? `#${o.id} — ${o.client_name} has already sent ${num(shipped)} case(s), so it cannot be deleted.`
    : `#${o.id} — ${o.client_name} (${num(o.total_qty)} cases). It leaves the pipeline and its stock goes back to Stock in Hand.`
})

async function doDelete() {
  const order = deleteOrder.value
  if (!order || deleteBusy.value) return
  deleteBusy.value = true
  deleteError.value = ''
  try {
    await api.del(`/orders/${order.id}/`)
    deleteOpen.value = false
    await load()
    ui.notify(`Order #${order.id} deleted — stock released`)
  } catch (e) {
    deleteError.value = e.message || 'Could not delete this order'
  } finally {
    deleteBusy.value = false
  }
}

onMounted(load)
</script>

<template>
  <v-app>
    <v-main>
      <v-container class="py-6" style="max-width: 900px">
        <div class="text-h6 font-weight-bold mb-1">Order status</div>
        <div class="text-body-2 text-medium-emphasis mb-4">
          Move an order's stock to Ready to Deliver, send it back to Pending, or
          take part of it across — both buttons open a picker where every line
          starts ticked, so moving the whole order is still a confirm. Orders
          that have sent nothing can also be deleted.
        </div>

        <v-alert
          v-if="error"
          type="error"
          variant="tonal"
          class="mb-4"
          density="compact"
        >
          {{ error }}
        </v-alert>

        <v-card class="mb-5" :loading="loading">
          <v-card-title class="d-flex align-center ga-3 py-3">
            <v-chip color="warning" variant="flat" size="small" class="font-weight-bold">
              {{ pending.length }}
            </v-chip>
            <span class="text-uppercase font-weight-bold text-medium-emphasis">
              Pending Orders
            </span>
          </v-card-title>
          <v-card-text class="pt-0">
            <v-list v-if="pending.length" density="compact">
              <v-list-item
                v-for="o in pending"
                :key="o.id"
                :title="`#${o.id} — ${o.client_name}`"
                :subtitle="subtitle(o)"
              >
                <template #append>
                  <v-btn
                    size="small"
                    color="primary"
                    variant="tonal"
                    prepend-icon="mdi-truck-fast-outline"
                    title="Move to Ready to Deliver, in full or in part"
                    @click="openMove(o, 'ready')"
                  >
                    Move to Ready
                  </v-btn>
                  <v-btn
                    icon="mdi-delete-outline"
                    size="small"
                    variant="text"
                    color="error"
                    title="Delete this pending order"
                    class="ml-1"
                    @click="openDelete(o)"
                  />
                </template>
              </v-list-item>
            </v-list>
            <div v-else class="text-body-2 text-medium-emphasis py-2">
              No pending orders.
            </div>
          </v-card-text>
        </v-card>

        <v-card :loading="loading">
          <v-card-title class="d-flex align-center ga-3 py-3">
            <v-chip color="success" variant="flat" size="small" class="font-weight-bold">
              {{ completed.length }}
            </v-chip>
            <span class="text-uppercase font-weight-bold text-medium-emphasis">
              Completed Orders
            </span>
          </v-card-title>
          <v-card-text class="pt-0">
            <v-list v-if="completed.length" density="compact">
              <v-list-item
                v-for="o in completed"
                :key="o.id"
                :title="`#${o.id} — ${o.client_name}`"
                :subtitle="subtitle(o)"
              >
                <template #append>
                  <v-chip
                    v-if="partDelivered(o)"
                    size="small"
                    color="info"
                    variant="tonal"
                    class="mr-2"
                  >
                    part delivered
                  </v-chip>
                  <v-btn
                    v-if="o.status !== 'DELIVERED'"
                    size="small"
                    variant="tonal"
                    prepend-icon="mdi-undo"
                    title="Move back to Pending, in full or in part"
                    @click="openMove(o, 'pending')"
                  >
                    Move to Pending
                  </v-btn>
                  <v-btn
                    v-if="o.status !== 'DELIVERED' && !partDelivered(o)"
                    icon="mdi-delete-outline"
                    size="small"
                    variant="text"
                    color="error"
                    title="Delete this order"
                    class="ml-1"
                    @click="openDelete(o)"
                  />
                  <v-chip v-if="o.status === 'DELIVERED'" size="small" color="success" variant="tonal">
                    Delivered
                  </v-chip>
                </template>
              </v-list-item>
            </v-list>
            <div v-else class="text-body-2 text-medium-emphasis py-2">
              No completed orders yet.
            </div>
          </v-card-text>
        </v-card>

        <!-- Same two dialogs Home uses: a partial move is chosen the same way
             wherever it is started, and a refused delete shows the server's
             reason here too. -->
        <OrderLinesDialog
          v-model="moveOpen"
          :title="moveTitle"
          :subtitle="moveSubtitle"
          :confirm-label="moveConfirmLabel"
          :color="moveColor"
          :icon="moveIcon"
          :lines="movableLines(moveOrder)"
          :busy="moveBusy"
          :error="moveError"
          @confirm="doMove"
        />

        <ConfirmDialog
          v-model="deleteOpen"
          title="Delete this order?"
          :text="deleteText"
          confirm-label="Delete order"
          icon="mdi-trash-can-outline"
          :busy="deleteBusy"
          :error="deleteError"
          @confirm="doDelete"
        />
      </v-container>
    </v-main>
  </v-app>
</template>

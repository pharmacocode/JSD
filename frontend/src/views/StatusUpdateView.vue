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
 */
import { ref, computed, onMounted } from 'vue'
import { api, ApiError } from '@/api'
import { num } from '@/utils/format'

const rows = ref([])
const loading = ref(true)
const error = ref('')
const busy = ref(0) // order id currently being written to

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
const completed = computed(() =>
  rows.value.filter((o) => o.status !== 'PENDING')
)

function skuSummary(order) {
  return (order.items || [])
    .map((i) => `${i.sku_description} × ${num(i.qty_cases)}`)
    .join(' · ')
}

async function setStatus(order, action, label) {
  busy.value = order.id
  error.value = ''
  try {
    const updated = await api.post(`/orders/${order.id}/${action}/`)
    // Replace the row in place so the two lists re-sort without a refetch.
    const index = rows.value.findIndex((o) => o.id === order.id)
    if (index >= 0) rows.value[index] = updated
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      const gaps = e.data?.shortages || []
      error.value =
        `#${order.id} — ${label} failed: ` +
        (gaps.length
          ? gaps
              .map(
                (g) =>
                  `${g.material}: need ${num(g.required)}, only ${num(
                    g.available
                  )} free`
              )
              .join('; ')
          : e.data?.detail || e.message)
    } else {
      error.value = e.message
    }
  } finally {
    busy.value = 0
  }
}

const complete = (order) => setStatus(order, 'complete', 'Mark complete')
const reopen = (order) => setStatus(order, 'reopen', 'Move back to Pending')

onMounted(load)
</script>

<template>
  <v-app>
    <v-main>
      <v-container class="py-6" style="max-width: 900px">
        <div class="text-h6 font-weight-bold mb-1">Order status</div>
        <div class="text-body-2 text-medium-emphasis mb-4">
          Mark an order complete to move its stock to Ready to Deliver, or send
          it back to Pending.
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
                :subtitle="`${num(o.total_qty)} cases · ${skuSummary(o)}`"
              >
                <template #append>
                  <v-btn
                    size="small"
                    color="primary"
                    variant="tonal"
                    prepend-icon="mdi-check"
                    :loading="busy === o.id"
                    :disabled="busy && busy !== o.id"
                    @click="complete(o)"
                  >
                    Mark complete
                  </v-btn>
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
                :subtitle="`${num(o.total_qty)} cases · ${skuSummary(o)}`"
              >
                <template #append>
                  <v-btn
                    v-if="o.status !== 'DELIVERED'"
                    size="small"
                    variant="tonal"
                    prepend-icon="mdi-undo"
                    :loading="busy === o.id"
                    :disabled="busy && busy !== o.id"
                    @click="reopen(o)"
                  >
                    Move to Pending
                  </v-btn>
                  <v-chip v-else size="small" color="success" variant="tonal">
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
      </v-container>
    </v-main>
  </v-app>
</template>

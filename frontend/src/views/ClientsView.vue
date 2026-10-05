<script setup>
/** Client list with persistent search (spec 4.5). */
import { ref, computed, onMounted, onActivated } from 'vue'
import { useRouter } from 'vue-router'
import { useRefDataStore } from '@/stores/refdata'
import { money } from '@/utils/format'
import EmptyState from '@/components/EmptyState.vue'

const router = useRouter()
const refdata = useRefDataStore()
const search = ref('')
const loading = ref(true)

async function load() {
  // Perf plan 2.8: the client list is shared through the reference store, so a
  // revisit paints instantly from cache and refreshes in the background instead
  // of blocking on the API round trip.
  if (refdata.loaded.clients) {
    loading.value = false
    refdata.fetch('clients', { force: true }).catch(() => {})
    return
  }
  loading.value = true
  try {
    await refdata.fetch('clients')
  } finally {
    loading.value = false
  }
}

// Live, in-memory filter — same fields the server searched (name / phone).
const clients = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return refdata.clients
  return refdata.clients.filter((c) =>
    [c.name, c.contact_number].some((v) => (v || '').toLowerCase().includes(q))
  )
})

onMounted(load)
// Fires on top of onMounted when this view is revived from the <keep-alive>
// cache (perf plan 2.10), so a revisit still revalidates the list.
onActivated(load)
</script>

<template>
  <div>
    <v-text-field
      v-model="search"
      prepend-inner-icon="mdi-magnify"
      label="Search clients…"
      clearable
      class="mb-2"
    />
    <v-btn
      color="primary"
      block
      class="mb-3"
      prepend-icon="mdi-plus"
      @click="router.push('/clients/new')"
    >
      New client
    </v-btn>

    <v-card :loading="loading">
      <EmptyState
        v-if="!loading && !clients.length"
        icon="mdi-account-group"
        text="No clients yet — tap + to add one."
      />
      <v-list v-else density="compact">
        <v-list-item
          v-for="c in clients"
          :key="c.id"
          :title="c.name"
          :subtitle="c.contact_number"
          :to="`/clients/${c.id}`"
          prepend-icon="mdi-account"
        >
          <template #append>
            <v-chip
              v-if="Number(c.pending_amount) > 0"
              color="error"
              size="small"
              variant="tonal"
            >
              {{ money(c.pending_amount) }}
            </v-chip>
            <v-chip v-else color="success" size="small" variant="tonal">
              settled
            </v-chip>
          </template>
        </v-list-item>
      </v-list>
    </v-card>
  </div>
</template>

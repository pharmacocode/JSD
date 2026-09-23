<script setup>
/** Client list with persistent search (spec 4.5). */
import { ref, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api, listify } from '@/api'
import { money } from '@/utils/format'
import EmptyState from '@/components/EmptyState.vue'

const router = useRouter()
const search = ref('')
const clients = ref([])
const loading = ref(true)

async function load() {
  loading.value = true
  try {
    clients.value = listify(await api.get('/clients/', { search: search.value }))
  } finally {
    loading.value = false
  }
}

onMounted(load)
let t = null
watch(search, () => {
  clearTimeout(t)
  t = setTimeout(load, 250) // debounce typing
})
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

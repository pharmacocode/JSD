<script setup>
/**
 * Edited / Deleted badge with expandable history (spec 3.7 — shown
 * wherever transactional records are displayed).
 */
import { ref } from 'vue'

const props = defineProps({
  record: { type: Object, required: true },
})
const open = ref(false)

function fmt(ts) {
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ts
  }
}
</script>

<template>
  <span v-if="record.is_deleted || record.is_edited" class="d-inline-block">
    <v-chip
      v-if="record.is_deleted"
      size="x-small"
      color="error"
      variant="flat"
      @click="open = !open"
    >
      Deleted
    </v-chip>
    <v-chip
      v-else
      size="x-small"
      color="warning"
      variant="tonal"
      @click="open = !open"
    >
      Edited
    </v-chip>
    <v-icon size="small" class="ml-1" @click="open = !open">
      mdi-history
    </v-icon>
    <v-expand-transition>
      <v-sheet
        v-if="open && record.edit_history?.length"
        class="mt-1 pa-2 text-caption"
        color="grey-lighten-3"
        rounded="lg"
      >
        <div
          v-for="(h, i) in record.edit_history"
          :key="i"
          class="mb-1"
        >
          <strong>{{ fmt(h.timestamp) }}</strong>
          <span v-if="h.action === 'deleted'"> — deleted</span>
          <template v-else>
            <div v-for="(v, k) in h.before" :key="k">
              {{ k }}: {{ v }} → {{ h.after?.[k] }}
            </div>
          </template>
        </div>
      </v-sheet>
    </v-expand-transition>
  </span>
</template>

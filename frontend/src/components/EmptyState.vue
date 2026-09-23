<script setup>
defineProps({
  icon: { type: String, default: 'mdi-information-outline' },
  text: { type: String, required: true },
  action: { type: String, default: '' },
})
defineEmits(['action'])
</script>

<template>
  <div class="text-center pa-8 text-medium-contrast">
    <v-icon size="48" color="grey">{{ icon }}</v-icon>
    <p class="mt-2 text-body-1">{{ text }}</p>
    <v-btn
      v-if="action"
      class="mt-3"
      color="primary"
      variant="tonal"
      @click="$emit('action')"
    >
      {{ action }}
    </v-btn>
  </div>
</template>

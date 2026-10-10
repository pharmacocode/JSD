<script setup>
/**
 * Small "are you sure?" dialog (user request: delete a pending order).
 *
 * Deliberately generic: the title and body say exactly what is about to be
 * lost — which order, how many cases, what happens to the stock — so the same
 * dialog reads correctly where it is used from (Home's two order panels and the
 * Order status board).
 *
 * The parent owns the API call, so it passes `busy` and `error` straight back
 * in: the dialog itself never talks to the backend, and a refused delete (an
 * order that has already shipped something) shows the server's reason inline
 * instead of closing as if it had worked.
 */
defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: 'Are you sure?' },
  text: { type: String, default: '' },
  confirmLabel: { type: String, default: 'Delete' },
  cancelLabel: { type: String, default: 'Cancel' },
  color: { type: String, default: 'error' },
  icon: { type: String, default: 'mdi-alert-outline' },
  busy: { type: Boolean, default: false },
  error: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue', 'confirm'])

const close = () => emit('update:modelValue', false)
</script>

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="460"
    @update:model-value="(open) => !open && !busy && close()"
  >
    <v-card>
      <v-card-title class="d-flex align-center text-subtitle-1 font-weight-bold">
        <v-icon start :color="color">{{ icon }}</v-icon>
        {{ title }}
      </v-card-title>
      <v-divider />
      <v-card-text>
        <p class="text-body-2 mb-0">{{ text }}</p>
        <!-- Extra detail the caller wants spelled out (the line list, the stock
             effect, ...) sits under the sentence rather than inside it. -->
        <slot />
        <v-alert
          v-if="error"
          type="error"
          variant="tonal"
          density="compact"
          class="mt-3"
        >
          {{ error }}
        </v-alert>
      </v-card-text>
      <v-divider />
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" :disabled="busy" @click="close">
          {{ cancelLabel }}
        </v-btn>
        <v-btn
          :color="color"
          variant="flat"
          :loading="busy"
          @click="emit('confirm')"
        >
          {{ confirmLabel }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

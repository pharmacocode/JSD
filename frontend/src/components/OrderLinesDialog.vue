<script setup>
/**
 * "Move cases between Pending and Ready to Deliver" dialog (user request).
 *
 * The SAME dialog does both directions, because the question is the same one:
 * how much of this order is crossing? Every line starts ticked at its full
 * quantity, so confirming without touching anything moves the WHOLE order —
 * the one-tap flow is still one tap — while a partial move is a tick and a
 * number away.
 *
 * Nothing is duplicated: the backend splits the order in two (the cases that
 * move get their own order, the rest stay put), so this component only decides
 * what moves and never invents stock arithmetic of its own.
 */
import { computed, ref, watch } from 'vue'
import { money, num } from '@/utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: 'Move cases' },
  subtitle: { type: String, default: '' },
  confirmLabel: { type: String, default: 'Confirm' },
  color: { type: String, default: 'primary' },
  icon: { type: String, default: 'mdi-truck-fast-outline' },
  /** [{ id, label, detail, qty, price }] — `qty` is the most that can move. */
  lines: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
  error: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue', 'confirm'])

/** id -> { on, take } — the working selection while the dialog is open. */
const picks = ref({})

function reset() {
  const next = {}
  for (const l of props.lines) next[l.id] = { on: true, take: Number(l.qty) }
  picks.value = next
}

// Reset on open, and whenever the dialog is reused for a different set of
// lines — never on a plain re-render, so a typed quantity is never clobbered.
const signature = computed(() => props.lines.map((l) => l.id).join(','))
watch(
  () => [props.modelValue, signature.value],
  ([open]) => {
    if (open) reset()
  },
  { immediate: true }
)

const chosen = computed(() =>
  props.lines.filter((l) => {
    const p = picks.value[l.id]
    return p && p.on && Number(p.take) > 0
  })
)

/** Lines whose ticked quantity is empty, zero or more than the line holds. */
const problems = computed(() =>
  props.lines.filter((l) => {
    const p = picks.value[l.id]
    if (!p || !p.on) return false
    const take = Number(p.take)
    return !(take > 0) || take > Number(l.qty)
  })
)

const cases = computed(() =>
  chosen.value.reduce((sum, l) => sum + Number(picks.value[l.id].take), 0)
)
const amount = computed(() =>
  chosen.value.reduce(
    (sum, l) => sum + Number(picks.value[l.id].take) * Number(l.price || 0),
    0
  )
)

/** Everything, in full — the caller then will not split the order at all. */
const whole = computed(
  () =>
    chosen.value.length === props.lines.length &&
    chosen.value.every((l) => Number(picks.value[l.id].take) === Number(l.qty))
)

const canConfirm = computed(
  () => !props.busy && chosen.value.length > 0 && problems.value.length === 0
)

const close = () => emit('update:modelValue', false)

function tickAll(on) {
  const next = {}
  for (const l of props.lines) {
    const p = picks.value[l.id] || { on: false, take: 0 }
    next[l.id] = { on, take: on ? Number(l.qty) : Number(p.take || 0) }
  }
  picks.value = next
}

function confirm() {
  if (!canConfirm.value) return
  emit(
    'confirm',
    chosen.value.map((l) => ({
      item: l.id,
      qty_cases: String(Number(picks.value[l.id].take)),
    }))
  )
}
</script>

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="620"
    persistent
    @update:model-value="(open) => !open && !busy && close()"
  >
    <v-card>
      <v-card-title class="d-flex align-center text-subtitle-1 font-weight-bold">
        <v-icon start :color="color">{{ icon }}</v-icon>
        {{ title }}
      </v-card-title>
      <div v-if="subtitle" class="text-body-2 text-medium-emphasis px-4 pb-2">
        {{ subtitle }}
      </div>
      <v-divider />

      <v-card-text>
        <div class="d-flex align-center mb-1">
          <span class="text-uppercase text-caption text-medium-emphasis">
            Take these cases
          </span>
          <v-spacer />
          <v-btn variant="text" size="small" :disabled="busy" @click="tickAll(true)">
            All
          </v-btn>
          <v-btn variant="text" size="small" :disabled="busy" @click="tickAll(false)">
            None
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
              {{ l.label }}
            </v-list-item-title>
            <v-list-item-subtitle class="text-caption">
              {{ l.detail }}
            </v-list-item-subtitle>
            <template #append>
              <div class="d-flex align-center ga-2">
                <v-text-field
                  v-model.number="picks[l.id].take"
                  type="number"
                  min="0"
                  :max="l.qty"
                  density="compact"
                  variant="outlined"
                  hide-details
                  single-line
                  style="max-width: 96px"
                  :disabled="busy || !picks[l.id].on"
                  :error="problems.includes(l)"
                />
                <span class="text-caption text-medium-emphasis text-no-wrap">
                  of {{ num(l.qty) }}
                </span>
              </div>
            </template>
          </v-list-item>
        </v-list>

        <div class="d-flex align-center ga-2 mt-4">
          <v-chip size="small" :color="color" variant="flat" class="font-weight-bold">
            {{ num(cases) }} cases
          </v-chip>
          <span class="text-body-2">{{ money(amount) }}</span>
          <v-spacer />
          <v-chip v-if="whole" size="small" variant="tonal" color="success">
            whole order
          </v-chip>
          <v-chip v-else size="small" variant="tonal" color="warning">
            partial — order splits
          </v-chip>
        </div>

        <p class="text-caption text-medium-emphasis mt-2 mb-0">
          <template v-if="whole">
            Every line moves, so nothing is left behind.
          </template>
          <template v-else>
            The cases left behind stay on this order exactly as they are — the
            cases you take get their own order, so nothing is counted twice.
          </template>
        </p>

        <v-alert
          v-if="problems.length"
          type="warning"
          variant="tonal"
          density="compact"
          class="mt-3"
        >
          A ticked line needs a quantity between 1 and the cases it holds
          ({{ problems.length }} line(s) to fix).
        </v-alert>
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
        <v-btn variant="text" :disabled="busy" @click="close">Cancel</v-btn>
        <v-btn
          :color="color"
          variant="flat"
          :loading="busy"
          :disabled="!canConfirm"
          @click="confirm"
        >
          {{ confirmLabel }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

import { defineStore } from 'pinia'
import { monthKey } from '@/utils/format'

/**
 * Shared UI state: the currently selected month (Home stats, Overheads),
 * lightweight toast feedback, and an overridable header back action so
 * multi-step screens (Add Delivery) can walk back through their own steps
 * instead of leaving the page.
 */
export const useUiStore = defineStore('ui', {
  state: () => ({
    month: monthKey(), // YYYY-MM
    toast: null, // { text, color }
    backAction: null, // optional () => void set by the active view
  }),
  actions: {
    setMonth(m) {
      this.month = m
    },
    notify(text, color = 'success') {
      this.toast = { text, color, at: Date.now() }
    },
    setBackAction(fn) {
      this.backAction = fn
    },
    clearBackAction() {
      this.backAction = null
    },
  },
})

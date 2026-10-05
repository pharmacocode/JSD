import { defineStore } from 'pinia'
import { monthKey } from '@/utils/format'

/**
 * Shared UI state: the currently selected month (Home stats, Overheads),
 * lightweight toast feedback, and an optional overridable header back action
 * that any view can register (leaving it unset falls back to browser history).
 */
export const useUiStore = defineStore('ui', {
  state: () => ({
    month: monthKey(), // YYYY-MM
    toast: null, // { text, color }
    backAction: null, // optional () => void set by the active view
    // Home dashboard payloads keyed by month, so navigating back to Home is
    // instant (perf plan 2.9) and only re-fetches in the background.
    dashboard: {},
  }),
  actions: {
    setMonth(m) {
      this.month = m
    },
    getDashboard(month) {
      return this.dashboard[month] || null
    },
    setDashboard(month, data) {
      this.dashboard[month] = data
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

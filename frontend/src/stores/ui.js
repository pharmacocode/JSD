import { defineStore } from 'pinia'
import { monthKey } from '@/utils/format'

/**
 * Shared UI state: the currently selected reporting period (Home stats),
 * lightweight toast feedback, and an optional overridable header back action
 * that any view can register (leaving it unset falls back to browser history).
 *
 * Period selection (user request): the month picker stays, but the dashboard
 * can also be driven by an explicit from/to date range. When BOTH dates are
 * set the range wins and the month is ignored entirely — see `rangeActive`.
 */
export const useUiStore = defineStore('ui', {
  state: () => ({
    month: monthKey(), // YYYY-MM
    // 'month' | 'range'. Chosen on the Home picker; Overheads stays month-only.
    periodMode: 'month',
    rangeFrom: null, // YYYY-MM-DD or null
    rangeTo: null, // YYYY-MM-DD or null
    toast: null, // { text, color }
    backAction: null, // optional () => void set by the active view
    // Home dashboard payloads keyed by periodKey, so navigating back to Home
    // is instant (perf plan 2.9) and only re-fetches in the background.
    dashboard: {},
  }),
  getters: {
    /**
     * True only when a complete, sane date range is selected — that is the
     * one case where the month must be ignored (user request: "if from and to
     * dates are given, then this month selection is not applicable").
     */
    rangeActive(state) {
      return (
        state.periodMode === 'range' &&
        !!state.rangeFrom &&
        !!state.rangeTo &&
        state.rangeFrom <= state.rangeTo
      )
    },
    /**
     * Cache key for the currently selected period. Mode is part of the key so
     * a month payload can never be served for a range, or vice versa.
     */
    periodKey(state) {
      return this.rangeActive
        ? `range:${state.rangeFrom}..${state.rangeTo}`
        : `month:${state.month}`
    },
  },
  actions: {
    setMonth(m) {
      this.month = m
    },
    setPeriodMode(mode) {
      this.periodMode = mode
    },
    setRange(from, to) {
      this.rangeFrom = from
      this.rangeTo = to
    },
    getDashboard(key = this.periodKey) {
      return this.dashboard[key] || null
    },
    setDashboard(key, data) {
      this.dashboard[key] = data
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

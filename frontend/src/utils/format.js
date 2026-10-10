/** Formatting helpers — INR currency, dates, months (spec Section 1). */

const inr = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  maximumFractionDigits: 2,
})

export function money(value) {
  const n = Number(value || 0)
  return inr.format(n)
}

export function num(value, digits = 2) {
  const n = Number(value || 0)
  return n.toLocaleString('en-IN', { maximumFractionDigits: digits })
}

export function today() {
  return new Date().toISOString().slice(0, 10)
}

export function monthKey(date = new Date()) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`
}

const MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
]

export function monthLabel(key) {
  if (!key) return ''
  const [y, m] = key.split('-')
  return `${MONTHS[Number(m) - 1]} ${y}`
}

/** 'Sep, 2026' — heading format for the Home summary (user request). */
export function monthLong(key) {
  if (!key) return ''
  const [y, m] = key.split('-')
  return `${MONTHS[Number(m) - 1]}, ${y}`
}

/**
 * '1 Jan 2026' — heading format for a single ISO date (the dashboard's
 * from/to range picker).
 */
export function dateLong(iso) {
  if (!iso) return ''
  const [y, m, d] = String(iso).slice(0, 10).split('-').map(Number)
  if (!y || !m || !d) return ''
  return `${d} ${MONTHS[m - 1]} ${y}`
}

/** '1 Jan 2026 → 31 Mar 2026' — the dashboard's range heading (user request). */
export function rangeLong(from, to) {
  return `${dateLong(from)} → ${dateLong(to)}`
}

export function fmtDate(d) {
  if (!d) return ''
  return String(d).slice(0, 10)
}

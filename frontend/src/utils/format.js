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

export function monthLabel(key) {
  if (!key) return ''
  const [y, m] = key.split('-')
  const names = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
  ]
  return `${names[Number(m) - 1]} ${y}`
}

export function fmtDate(d) {
  if (!d) return ''
  return String(d).slice(0, 10)
}

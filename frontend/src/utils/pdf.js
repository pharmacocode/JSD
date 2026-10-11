/**
 * PDF rendering for the two exports (user request):
 *
 *   exportClientStatement — delivery-by-delivery statement for one client
 *     (Client detail → Deliveries → Export PDF): the period's consignment
 *     lines with their revenue, the payments received in the period (with
 *     the balance carried in and the resulting total due). Client-facing, so
 *     it deliberately carries NO cost or profit figures (user request).
 *   exportDeliveryRegister — the Home delivery register for any date range:
 *     date / client / SKU / qty / revenue / profit with totals.
 *
 * jsPDF + jspdf-autotable are loaded LAZILY (dynamic import) so the router
 * chunks stay small and the library only lands in the browser when someone
 * actually exports. Both functions resolve once the download is triggered.
 *
 * NOTE on glyphs: jsPDF's core fonts are WinAnsi-only, so ₹ (money) and →
 * are never written straight in — pdfMoney()/dateLong text renders ASCII
 * ("Rs. ", "to") instead, or the glyph would come out blank.
 */
import { money, num, dateLong } from '@/utils/format'

const BRAND = 'JSD Customised Bottles'
const MARGIN = 14
const RIGHT = 196 // A4 width 210 − 2 × 14

/** ₹ → "Rs. " so the core fonts can render the figure. */
function pdfMoney(v) {
  return money(v).replace(/₹/g, 'Rs. ')
}

function slug(text) {
  return (
    String(text || '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'client'
  )
}

/** Lazy-load jsPDF + the autoTable plugin (shared by both exports). */
async function toolkit() {
  const [{ jsPDF }, mod] = await Promise.all([
    import('jspdf'),
    import('jspdf-autotable'),
  ])
  // jspdf-autotable v5 exposes the callable as the default export; older
  // builds export it as a name — accept either so a lockfile bump cannot
  // silently break both exports.
  const autoTable = mod.default || mod.autoTable || mod
  return { doc: new jsPDF(), autoTable }
}

/** Brand header + subtitle; returns the Y the content starts at. */
function heading(doc, subtitle) {
  doc.setFont('helvetica', 'bold')
  doc.setFontSize(15)
  doc.setTextColor(20)
  doc.text(BRAND, MARGIN, 16)
  doc.setFont('helvetica', 'normal')
  doc.setFontSize(10)
  doc.setTextColor(110)
  doc.text(subtitle, MARGIN, 22)
  doc.setDrawColor(200)
  doc.line(MARGIN, 25, RIGHT, 25)
  doc.setTextColor(20)
  return 31
}

/** Period + generated line under the heading; returns the next Y. */
function periodLine(doc, from, to, y, generatedAt) {
  doc.setFontSize(9)
  doc.setTextColor(110)
  doc.text(`Period: ${dateLong(from)} to ${dateLong(to)}`, MARGIN, y)
  doc.text(
    `Generated: ${dateLong(generatedAt || new Date().toISOString())}`,
    RIGHT,
    y,
    { align: 'right' },
  )
  doc.setTextColor(20)
  return y + 7
}

/** Shared autoTable chrome: blue header, right-aligned numeric columns. */
const TABLE_STYLE = {
  styles: { fontSize: 9, cellPadding: 2.2, textColor: [30, 30, 30] },
  headStyles: { fillColor: [25, 103, 192], textColor: 255 },
  footStyles: {
    fillColor: [238, 242, 250],
    textColor: [30, 30, 30],
    fontStyle: 'bold',
  },
  margin: { left: MARGIN, right: MARGIN },
}

/**
 * Client delivery statement. `statement` is the payload of
 * GET /api/clients/<id>/statement/?from=&to= — see ClientViewSet.statement.
 */
export async function exportClientStatement(statement) {
  const { doc, autoTable } = await toolkit()
  let y = heading(doc, 'Delivery statement')

  doc.setFont('helvetica', 'bold')
  doc.setFontSize(13)
  doc.text(String(statement.client_name || 'Client'), MARGIN, y + 2)
  doc.setFont('helvetica', 'normal')
  y = periodLine(doc, statement.from, statement.to, y + 9, statement.generated_at)

  // Client-facing document: revenue only. Cost and profit figures never
  // leave the owner's screens (user request).
  const rows = statement.deliveries || []
  const totalQty = rows.reduce((s, r) => s + Number(r.qty_cases || 0), 0)
  autoTable(doc, {
    ...TABLE_STYLE,
    startY: y,
    head: [['Date', 'SKU', 'Qty', 'Rate / case', 'Amount']],
    body: rows.map((r) => [
      r.date,
      r.sku,
      num(r.qty_cases),
      pdfMoney(r.selling_price_per_case),
      pdfMoney(r.line_amount),
    ]),
    foot: [['', 'Total', num(totalQty), '', pdfMoney(statement.period_revenue)]],
    columnStyles: {
      2: { halign: 'right' },
      3: { halign: 'right' },
      4: { halign: 'right' },
    },
  })
  y = (doc.lastAutoTable?.finalY ?? y + 40) + 12

  // Payments received inside the period, itemised (date / note / amount).
  const payments = statement.payments || []
  if (payments.length) {
    doc.setFont('helvetica', 'bold')
    doc.setFontSize(10)
    doc.text('Payments received in this period', MARGIN, y)
    doc.setFont('helvetica', 'normal')
    autoTable(doc, {
      ...TABLE_STYLE,
      startY: y + 4,
      head: [['Date', 'Note', 'Amount']],
      body: payments.map((p) => [p.date, p.note || '', pdfMoney(p.amount)]),
      columnStyles: { 2: { halign: 'right' } },
    })
    y = (doc.lastAutoTable?.finalY ?? y + 20) + 12
  }

  doc.setFillColor(245, 247, 250)
  doc.roundedRect(MARGIN, y - 8, RIGHT - MARGIN, 42, 2, 2, 'F')
  y = summaryRow(
    doc,
    'Billed in this period',
    pdfMoney(statement.period_revenue),
    y,
  )
  y = summaryRow(
    doc,
    'Payments received in this period',
    `- ${pdfMoney(statement.payments_total)}`,
    y,
  )
  y = summaryRow(
    doc,
    `Balance carried in (before ${dateLong(statement.from)})`,
    pdfMoney(statement.previous_due),
    y,
  )
  y = summaryRow(doc, 'Total due after this period', pdfMoney(statement.total_due), y, true)

  doc.setFontSize(8)
  doc.setTextColor(110)
  doc.text(
    'Total due = balance carried in + period revenue - period payments.',
    MARGIN + 4,
    y,
  )
  doc.setTextColor(20)

  footers(doc, 'Delivery statement')
  const file = `JSD-statement-${slug(statement.client_name)}-${statement.from}-to-${statement.to}.pdf`
  doc.save(file)
  return file
}

/**
 * Home delivery register. `opts`: { from, to, rows, profitLabel, profitNote }
 * — each row carries date / client_name / sku_description / qty_cases /
 * total_amount and `_profit`, computed by the caller so the Home profit
 * toggle (with/without overhead) flows into the PDF unchanged.
 */
export async function exportDeliveryRegister(opts) {
  const { doc, autoTable } = await toolkit()
  const { from, to, rows = [], profitLabel = 'Profit' } = opts

  let y = heading(doc, 'Delivery register')
  y = periodLine(doc, from, to, y + 4)

  const totalQty = rows.reduce((s, r) => s + Number(r.qty_cases || 0), 0)
  const totalRevenue = rows.reduce((s, r) => s + Number(r.total_amount || 0), 0)
  const totalProfit = rows.reduce((s, r) => s + Number(r._profit || 0), 0)

  autoTable(doc, {
    ...TABLE_STYLE,
    startY: y,
    head: [['Date', 'Client', 'SKU', 'Qty', 'Revenue', profitLabel]],
    body: rows.map((r) => [
      r.date,
      r.client_name,
      r.sku_description,
      num(r.qty_cases),
      pdfMoney(r.total_amount),
      pdfMoney(r._profit),
    ]),
    foot: [
      ['', '', 'Total', num(totalQty), pdfMoney(totalRevenue), pdfMoney(totalProfit)],
    ],
    columnStyles: {
      3: { halign: 'right' },
      4: { halign: 'right' },
      5: { halign: 'right' },
    },
  })
  y = (doc.lastAutoTable?.finalY ?? y + 40) + 8

  doc.setFontSize(8)
  doc.setTextColor(110)
  doc.text(
    opts.profitNote ||
      'Profit = revenue - direct cost (current FIFO materials + print).',
    MARGIN,
    y,
  )
  doc.setTextColor(20)

  footers(doc, 'Delivery register')
  const file = `JSD-delivery-register-${from}-to-${to}.pdf`
  doc.save(file)
  return file
}

/** Two-column label/figure block; returns the next Y. */
function summaryRow(doc, label, value, y, bold = false) {
  doc.setFontSize(10)
  doc.setFont('helvetica', bold ? 'bold' : 'normal')
  doc.text(label, MARGIN + 4, y)
  doc.text(value, RIGHT - 4, y, { align: 'right' })
  doc.setFont('helvetica', 'normal')
  return y + 6
}

/** Page footers on every page (added once, at the end). */
function footers(doc, note) {
  const pages = doc.internal.getNumberOfPages()
  doc.setFontSize(8)
  doc.setTextColor(150)
  for (let i = 1; i <= pages; i++) {
    doc.setPage(i)
    doc.text(`${BRAND} · page ${i} of ${pages}`, MARGIN, 288)
    doc.text(note, RIGHT, 288, { align: 'right' })
  }
  doc.setTextColor(20)
}

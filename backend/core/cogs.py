"""
FIFO batch costing engine + COGS calculation — ALL PER CASE (spec Sections
5 & 6, updated per user instruction: bottles are never considered anywhere;
every quantity, price and cost is expressed per case).

Key rules:
- At CREATION, cost follows the physical batch consumed: the snapshot records
  the weighted cost of the specific oldest batch(es) actually consumed.
  September deliveries can still carry "August pricing" while August stock
  remains in the queue.
- Displayed costs are DYNAMIC everywhere (user request — no frozen costs):
  raw material cost is recomputed on every read against TODAY's FIFO queue
  (dynamic_costs_for -> _preview_fifo_unit_cost, non-mutating) and print/label
  cost against the SKU's CURRENT print config, so price, batch and
  print-config edits move every chart, drill-down and delivery row
  immediately. StockDelivery.base_cogs_per_case_snapshot /
  cogs_per_case_snapshot keep the creation-time figures for the AUDIT trail
  only — nothing reads them for display.
- The OVERHEAD part is dynamic too: the displayed per-case COGS is
  current direct cost + the delivery month's CURRENT overhead allocation,
  so later overhead/labour edits for that month flow through to it.
- Material quantities (batch receipts, stock in hand, SKU requirements) are
  all in CASES of that material.
- Overhead per case = total_monthly_overhead / total_cases_sold_that_month —
  LIVE (no month-close lock): it recomputes as overhead entries and new
  deliveries land in that month.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from .models import (
    Client,
    ClientLedgerEntry,
    MaterialBatch,
    SKUPrintCost,
    StockDelivery,
    money,
    qty as round_qty,
)

ZERO = Decimal("0")


def month_key(date_obj) -> str:
    """YYYY-MM for a date."""
    return date_obj.strftime("%Y-%m")


def resolve_requirements(sku, client: Client):
    """
    Resolve a SKU's material requirements for a specific client.

    Returns list of (material, qty_per_case) — cases of material needed per
    case of the finished SKU.

    Resolution rules:
    - Non-client-specific materials are used as-is.
    - A client-specific material belonging to this client is used as-is.
    - A GENERIC label material (category='Label', not client-specific) is
      substituted with this client's own label material when one exists
      (custom labels are per-client stock — spec 3.3 / 4.2 step 4).
    """
    from .models import Material

    resolved = []
    for req in sku.requirements.select_related("material"):
        mat = req.material
        if mat.is_client_specific:
            if mat.client_id == client.id:
                resolved.append((mat, req.qty_per_case))
            # Client-specific material for a DIFFERENT client never applies
            # to this delivery — skip rather than consume the wrong labels.
            continue
        if mat.category == "Label":
            client_label = (
                Material.objects.filter(
                    is_client_specific=True, client=client, category="Label"
                )
                .order_by("id")
                .first()
            )
            if client_label:
                resolved.append((client_label, req.qty_per_case))
                continue
        resolved.append((mat, req.qty_per_case))
    return resolved


def fifo_batches_for(material):
    """Oldest-first queue of live batches (soft-deleted excluded)."""
    return material.batches.filter(is_deleted=False).order_by("arrival_date", "id")


def stock_available(material) -> Decimal:
    return (
        fifo_batches_for(material).aggregate(s=Sum("quantity_remaining"))["s"]
        or ZERO
    )


def check_shortfall(sku, client, qty_cases: Decimal):
    """
    Without mutating the DB, check stock sufficiency for every required
    material (all in cases). Returns a list of shortfall dicts:
      {material_id, material, required, available, short_by, unit}
    """
    return check_shortfall_many(client, [(sku, Decimal(qty_cases))])


def check_shortfall_many(client, lines):
    """
    Multi-SKU sufficiency check (spec 4.2 step 4) without mutating the DB.

    `lines` = [(sku, qty_cases), ...] for ONE delivery (same client/date).

    Requirements are summed PER MATERIAL across all lines before being
    compared with stock: two SKUs can draw on the same raw material, and two
    SKUs can resolve to the same client-owned label, so a delivery that only
    fits when its lines are considered together must not be reported short.
    Each line is rounded on its own — exactly what FIFO consumption will take.

    Returns the same list shape as the single-line check.
    """
    required_by_material = {}
    order = []
    for sku, qty_cases in lines:
        qty_cases = Decimal(qty_cases)
        for material, per_case in resolve_requirements(sku, client):
            required = round_qty(qty_cases * per_case)
            if material.id in required_by_material:
                required_by_material[material.id] += required
            else:
                required_by_material[material.id] = required
                order.append(material)

    shortages = []
    for material in order:
        required = required_by_material[material.id]
        available = stock_available(material)
        if available < required:
            shortages.append(
                {
                    "material_id": material.id,
                    "material": material.name,
                    "required": str(required),
                    "available": str(available),
                    "short_by": str(round_qty(required - available)),
                    "unit": material.unit_of_measure,
                }
            )
    return shortages


def consume_fifo(
    material, units: Decimal, allow_negative: bool = True, deficit_date=None
):
    """
    Consume `units` (cases) from `material`'s queue, oldest batch first.
    Rolls over to the next-oldest batch when the current one is
    insufficient (spec 5).

    Returns (consumption_list, total_cost, unrecorded_shortfall):
      consumption_list: [{"batch_id", "qty", "price_per_unit", ...} ...]
      total_cost: weighted cost of the batch(es) actually consumed — the
        cost that feeds the delivery's COGS snapshot.
      unrecorded_shortfall: units that could NOT be booked against any batch
        (only when allow_negative=False).

    When allow_negative=True (delivery flow, spec 4.2 step 4) a deficit is
    ALWAYS booked, so stock in hand goes NEGATIVE and stays reconcilable
    (user request: a delivery marked when there is no stock must mark stock
    in hand negative, prompting a Stock Adjustment or a retrospective /
    backdated arrival):
    - partially consumed batches are pushed negative (oldest first),
    - an already-exhausted queue is pushed further negative on its OLDEST
      batch,
    - a material with NO batches at all gets a zero-received "deficit
      carrier" batch (dated `deficit_date`, priced at the master display
      price — what the Add-Delivery preview already quotes for an empty
      queue) whose remaining is negative.
    Such deliveries are flagged stock_shortfall_flag=True. StockAdjustment
    and keyed batch edits are the only other writer paths — batches are
    never silently edited elsewhere.
    """
    units = Decimal(units)
    consumption = []
    total_cost = ZERO
    remaining = units
    batches = list(fifo_batches_for(material))
    touched = []

    # Phase 1 — consume positive stock, oldest first.
    for batch in batches:
        if remaining <= 0:
            break
        if batch.quantity_remaining > 0:
            take = min(batch.quantity_remaining, remaining)
            batch.quantity_remaining -= take
            consumption.append(
                {
                    "batch_id": batch.id,
                    "qty": str(round_qty(take)),
                    "price_per_unit": str(batch.price_per_unit),
                }
            )
            total_cost += take * batch.price_per_unit
            remaining -= take
            touched.append(batch)

    # Phase 2 — deficit handling.
    unrecorded_shortfall = ZERO
    if remaining > 0:
        if allow_negative and touched:
            # Push deficit oldest-first onto the batches we just exhausted.
            for batch in touched:
                if remaining <= 0:
                    break
                batch.quantity_remaining -= remaining  # goes negative
                consumption.append(
                    {
                        "batch_id": batch.id,
                        "qty": str(round_qty(remaining)),
                        "price_per_unit": str(batch.price_per_unit),
                        "deficit": True,
                    }
                )
                total_cost += remaining * batch.price_per_unit
                remaining = ZERO
        elif allow_negative and batches:
            # Queue exists but has nothing positive left — extend the deficit
            # on the OLDEST batch so stock in hand keeps showing the true
            # negative figure (each further delivery deepens it visibly).
            batch = batches[0]
            batch.quantity_remaining -= remaining  # goes negative
            consumption.append(
                {
                    "batch_id": batch.id,
                    "qty": str(round_qty(remaining)),
                    "price_per_unit": str(batch.price_per_unit),
                    "deficit": True,
                }
            )
            total_cost += remaining * batch.price_per_unit
            batch.save(skip_audit=True)
            remaining = ZERO
        elif allow_negative:
            # No batch rows at all — book the deficit on a carrier batch
            # (received 0, remaining negative) instead of silently leaving
            # stock at 0. Priced at the master display price, which is what
            # the Add-Delivery preview already quotes for an empty queue.
            carrier = MaterialBatch(
                material=material,
                quantity_received=ZERO,
                quantity_remaining=-remaining,
                price_per_unit=material.current_price_per_unit,
                arrival_date=deficit_date or timezone_now_date(),
                note=(
                    "Stock shortfall booking — reconcile via Stock "
                    "Adjustment or a backdated stock arrival."
                ),
            )
            carrier.save(skip_audit=True)
            consumption.append(
                {
                    "batch_id": carrier.id,
                    "qty": str(round_qty(remaining)),
                    "price_per_unit": str(carrier.price_per_unit),
                    "deficit": True,
                }
            )
            total_cost += remaining * carrier.price_per_unit
            remaining = ZERO
        else:
            unrecorded_shortfall = round_qty(remaining)

    for batch in touched:
        # FIFO consumption bookkeeping is not a manual "edit" — skip audit.
        batch.save(skip_audit=True)

    return consumption, total_cost, unrecorded_shortfall


def overhead_for_month(month: str) -> Decimal:
    """Sum of all overhead categories for a YYYY-MM month (incl. labour)."""
    from .models import MonthlyOverhead

    return (
        MonthlyOverhead.objects.filter(month=month).aggregate(s=Sum("amount"))["s"]
        or ZERO
    )


def cases_sold_in_month(month: str, include_cases: Decimal = ZERO) -> Decimal:
    """Total non-deleted cases sold in a YYYY-MM month (+ optional extra)."""
    year, mon = (int(p) for p in month.split("-"))
    qs = StockDelivery.objects.filter(
        is_deleted=False, date__year=year, date__month=mon
    )
    return (qs.aggregate(s=Sum("qty_cases"))["s"] or ZERO) + Decimal(include_cases)


def overhead_per_case(month: str, additional_cases: Decimal = ZERO) -> Decimal:
    """
    total_monthly_overhead / total_cases_sold_that_month (spec 6.3, now
    purely per-case). LIVE estimate — recomputed on each new delivery in
    that month (confirmed assumption 1, no month-close lock). 0 if no sales.
    """
    sold = cases_sold_in_month(month, include_cases=additional_cases)
    if sold <= 0:
        return ZERO
    return overhead_for_month(month) / sold


def print_cost_per_case(sku) -> tuple:
    """
    Spec 6.2 adapted to per-case: one case needs sku.qty_per_case labels.
    per-case print cost = ((paper_cost + print_cost_per_paper)
    / labels_per_paper) x qty_per_case x (1 + wastage_percent/100).
    Returns (cost, breakdown_dict). Missing config => zero cost.
    """
    try:
        pc = sku.print_cost
    except SKUPrintCost.DoesNotExist:
        return ZERO, None
    if not pc.labels_per_paper:
        return ZERO, None
    per_label = (pc.paper_cost + pc.print_cost_per_paper) / pc.labels_per_paper
    labels_per_case = sku.qty_per_case
    adjusted = (
        per_label * labels_per_case * (Decimal("1") + pc.wastage_percent / Decimal("100"))
    )
    return adjusted, {
        "paper_cost": str(pc.paper_cost),
        "print_cost_per_paper": str(pc.print_cost_per_paper),
        "labels_per_paper": str(pc.labels_per_paper),
        "wastage_percent": str(pc.wastage_percent),
        "labels_per_case": str(labels_per_case),
        "cost_per_case": str(money(adjusted)),
    }


def estimate_cogs(sku, client, qty_cases: Decimal, delivery_date, preview=True):
    """
    Spec 6 — compute COGS PER CASE for a prospective delivery. No per-bottle
    values are ever produced (user requirement: cases only, everywhere).

    preview=True  -> non-mutating estimate (Add-Delivery screen, breakup).
    preview=False -> performs actual FIFO consumption (create_delivery) and
      returns the real weighted cost from the batches consumed.

    Returns dict: {month, per_case, details:{materials, print, overhead},
    shortfall, consumption, has_unrecorded_shortfall}
    """
    month = month_key(delivery_date)
    qty_cases = Decimal(qty_cases)

    material_lines = []
    raw_total_for_delivery = ZERO
    shortfall = check_shortfall(sku, client, qty_cases) if preview else []
    consumption_log = []
    has_unrecorded_shortfall = False

    for material, per_case_qty in resolve_requirements(sku, client):
        required = qty_cases * per_case_qty  # cases of this material
        # Per-material wastage (spec 6.1): x (1 + wastage_percent/100).
        wastage_factor = Decimal("1") + material.wastage_percent / Decimal("100")
        if preview:
            unit_cost = _preview_fifo_unit_cost(material, required)
            line_cost = required * unit_cost * wastage_factor
        else:
            consumption, total_cost, unrecorded = consume_fifo(
                material,
                round_qty(required),
                allow_negative=True,
                deficit_date=delivery_date,
            )
            consumption_log.append(
                {"material_id": material.id, "consumed": consumption}
            )
            if unrecorded > 0:
                has_unrecorded_shortfall = True
            # Cost follows the actual batch(es) consumed (spec 5).
            unit_cost = (total_cost / required) if required > 0 else ZERO
            line_cost = total_cost * wastage_factor
        raw_total_for_delivery += line_cost
        material_lines.append(
            {
                "material_id": material.id,
                "material": material.name,
                "unit": material.unit_of_measure,
                "qty_per_case": str(per_case_qty),
                "qty_required": str(round_qty(required)),
                "fifo_unit_cost": str(unit_cost),
                "wastage_percent": str(material.wastage_percent),
                "line_cost": str(money(line_cost)),
                "line_cost_per_case": str(
                    money(line_cost / qty_cases) if qty_cases > 0 else money(0)
                ),
            }
        )

    raw_per_case = raw_total_for_delivery / qty_cases if qty_cases > 0 else ZERO
    print_per_case, print_breakdown = print_cost_per_case(sku)

    # Base (direct) cost per case = materials + print. Recorded on the
    # delivery for the audit trail; every later display recomputes it
    # dynamically from current prices (dynamic_costs_for), and overhead is
    # added dynamically on top.
    base_per_case = raw_per_case + print_per_case

    # Spec 6.3 per case: month overhead / cases sold this month (including
    # this delivery — it is not saved yet, so added explicitly in BOTH modes).
    oh_per_case = overhead_per_case(month, additional_cases=qty_cases)

    total_per_case = base_per_case + oh_per_case

    return {
        "month": month,
        "per_case": str(money(total_per_case)),
        "per_case_base": str(money(base_per_case)),
        "overhead_per_case": str(money(oh_per_case)),
        "details": {
            "materials": material_lines,
            "print": print_breakdown,
            "overhead": {
                "month": month,
                "total_monthly_overhead": str(money(overhead_for_month(month))),
                "cases_sold_in_month": str(
                    cases_sold_in_month(month, include_cases=qty_cases)
                ),
                "overhead_per_case": str(money(oh_per_case)),
                "note": (
                    "LIVE estimate — recomputed as more deliveries land this "
                    "month (confirmed assumption, no month-close lock in v1)."
                ),
            },
        },
        "shortfall": shortfall,
        "consumption": consumption_log,
        "has_unrecorded_shortfall": has_unrecorded_shortfall,
    }


def _preview_fifo_unit_cost(material, units_needed: Decimal) -> Decimal:
    """
    Non-mutating weighted unit cost if we were to consume `units_needed`
    from the FIFO queue right now (walks batches oldest-first, no writes).
    Falls back to the newest batch price when the queue is empty.
    """
    remaining = Decimal(units_needed)
    total_cost = ZERO
    total_taken = ZERO
    last_price = None
    for batch in fifo_batches_for(material):
        last_price = batch.price_per_unit
        if remaining <= 0:
            break
        available = batch.quantity_remaining
        if available > 0:
            take = min(available, remaining)
            total_cost += take * batch.price_per_unit
            total_taken += take
            remaining -= take
    if remaining > 0:
        # Deficit units priced at newest known batch price for the estimate.
        price = last_price if last_price is not None else material.current_price_per_unit
        total_cost += remaining * price
        total_taken += remaining
    if total_taken <= 0:
        return material.current_price_per_unit
    return total_cost / total_taken


def dynamic_costs_for(deliveries) -> dict:
    """
    CURRENT direct cost (raw materials + print) for the given deliveries,
    recomputed at READ time — no frozen snapshots (user request: every chart,
    drill-down and delivery row must react immediately to price, batch and
    print-config edits).

    Materials: each material's required quantity is summed ACROSS the given
    deliveries and today's FIFO queue is walked ONCE for that total
    (non-mutating), so the returned rows carry the true blended replacement
    cost — cheap stock is never double-counted per delivery, and any totals
    derived from the rows add back up exactly (chart bars vs drill-down rows).
    Deficit units price at the newest known batch / master price, exactly like
    the Add-Delivery preview. Print is the SKU's CURRENT print config (never
    FIFO), so a config edit flows through instantly.

    Returns {delivery.id: {"materials": Decimal, "print": Decimal,
                           "direct": Decimal}} — raw Decimals; callers money()
    them at the edges.
    """
    deliveries = list(deliveries)

    # 1. Requirements per delivery + physical totals per material.
    lines_per_delivery = []
    required_by_material = {}
    for d in deliveries:
        lines = []
        for material, per_case_qty in resolve_requirements(d.sku, d.client):
            required = d.qty_cases * per_case_qty
            lines.append((material, required))
            if required > 0:
                prev = required_by_material.get(material.id)
                if prev:
                    required_by_material[material.id] = (prev[0], prev[1] + required)
                else:
                    required_by_material[material.id] = (material, required)
        lines_per_delivery.append(lines)

    # 2. Today's queue, walked once per material for the whole set.
    unit_costs = {
        material_id: _preview_fifo_unit_cost(material, total)
        for material_id, (material, total) in required_by_material.items()
    }

    # 3. Apply per delivery (+ per-material wastage, + current print config).
    out = {}
    for d, lines in zip(deliveries, lines_per_delivery):
        materials_cost = ZERO
        for material, required in lines:
            wastage_factor = (
                Decimal("1") + material.wastage_percent / Decimal("100")
            )
            unit_cost = unit_costs.get(material.id, material.current_price_per_unit)
            materials_cost += required * unit_cost * wastage_factor
        print_pc, _print_breakdown = print_cost_per_case(d.sku)
        print_total = d.qty_cases * print_pc
        out[d.id] = {
            "materials": materials_cost,
            "print": print_total,
            "direct": materials_cost + print_total,
        }
    return out


def preview_deliveries(client, lines, delivery_date):
    """
    Non-mutating COGS + shortfall preview for a MULTI-SKU delivery (spec 4.2 /
    Section 6, everything per case). `lines` = [{"sku": sku, "qty_cases": Decimal}].

    A material needed by several lines (same raw material, or two SKUs that
    both resolve to the client's own label) is priced ONCE against the FIFO
    queue for the delivery's combined requirement, and that same unit cost is
    used for each line — so the second SKU is never quoted from the front of
    the queue a second time.

    Overhead is allocated for the delivery as a whole: the month's pool /
    (cases already sold that month + this delivery's cases).

    Returns {"month", "lines": [per-line breakdown], "cogs": {aggregate
    per-case + overhead pool}, "totals": {cases, base_cogs, cogs},
    "shortfall": [shortfall rows summed per material across the lines]}.
    """
    month = month_key(delivery_date)
    total_cases = sum((Decimal(line["qty_cases"]) for line in lines), ZERO)
    if total_cases <= 0:
        return {
            "month": month,
            "lines": [],
            "cogs": {
                "per_case": str(money(0)),
                "base_per_case": str(money(0)),
                "overhead_per_case": str(money(0)),
                "materials_per_case": str(money(0)),
                "print_per_case": str(money(0)),
                "details": {"overhead": None},
            },
            "totals": {
                "cases": "0",
                "base_cogs": str(money(0)),
                "cogs": str(money(0)),
            },
            "shortfall": [],
        }

    # 1. Requirements per line + the combined requirement per material.
    per_line_reqs = []
    materials_by_id = {}
    required_totals = {}
    for line in lines:
        qty_cases = Decimal(line["qty_cases"])
        rows = []
        for material, per_case in resolve_requirements(line["sku"], client):
            required = round_qty(qty_cases * per_case)
            rows.append((material, required, per_case))
            materials_by_id[material.id] = material
            required_totals[material.id] = (
                required_totals.get(material.id, ZERO) + required
            )
        per_line_reqs.append(rows)

    # 2. One FIFO unit cost per material for the whole delivery.
    unit_costs = {
        material_id: _preview_fifo_unit_cost(materials_by_id[material_id], required)
        for material_id, required in required_totals.items()
    }

    oh_per_case = overhead_per_case(month, additional_cases=total_cases)
    # 3. Per-line breakdown; shared material costs are split back out.
    out_lines = []
    material_cost_total = ZERO
    print_cost_total = ZERO
    for line, rows in zip(lines, per_line_reqs):
        qty_cases = Decimal(line["qty_cases"])
        material_lines = []
        line_material_cost = ZERO
        for material, required, per_case in rows:
            unit_cost = unit_costs[material.id]
            wastage_factor = Decimal("1") + material.wastage_percent / Decimal("100")
            line_cost = required * unit_cost * wastage_factor
            line_material_cost += line_cost
            material_lines.append(
                {
                    "material_id": material.id,
                    "material": material.name,
                    "unit": material.unit_of_measure,
                    "qty_per_case": str(per_case),
                    "qty_required": str(required),
                    "fifo_unit_cost": str(unit_cost),
                    "wastage_percent": str(material.wastage_percent),
                    "line_cost": str(money(line_cost)),
                    "line_cost_per_case": str(
                        money(line_cost / qty_cases) if qty_cases > 0 else money(0)
                    ),
                }
            )

        print_per_case, print_breakdown = print_cost_per_case(line["sku"])
        material_per_case = line_material_cost / qty_cases
        base_per_case = material_per_case + print_per_case
        material_cost_total += line_material_cost
        print_cost_total += print_per_case * qty_cases
        out_lines.append(
            {
                "sku": line["sku"].id,
                "sku_description": line["sku"].description,
                "qty_cases": str(round_qty(qty_cases)),
                "materials_per_case": str(money(material_per_case)),
                "print_per_case": str(money(print_per_case)),
                "base_per_case": str(money(base_per_case)),
                "overhead_per_case": str(money(oh_per_case)),
                "per_case": str(money(base_per_case + oh_per_case)),
                "total_cogs": str(money((base_per_case + oh_per_case) * qty_cases)),
                "details": {"materials": material_lines, "print": print_breakdown},
            }
        )

    base_total = material_cost_total + print_cost_total
    base_per_case = base_total / total_cases
    total_per_case = base_per_case + oh_per_case
    return {
        "month": month,
        "lines": out_lines,
        "cogs": {
            "per_case": str(money(total_per_case)),
            "base_per_case": str(money(base_per_case)),
            "overhead_per_case": str(money(oh_per_case)),
            "materials_per_case": str(money(material_cost_total / total_cases)),
            "print_per_case": str(money(print_cost_total / total_cases)),
            "details": {
                "overhead": {
                    "month": month,
                    "total_monthly_overhead": str(money(overhead_for_month(month))),
                    "cases_sold_in_month": str(
                        cases_sold_in_month(month, include_cases=total_cases)
                    ),
                    "overhead_per_case": str(money(oh_per_case)),
                    "note": (
                        "LIVE estimate — recomputed as more deliveries land this "
                        "month (confirmed assumption, no month-close lock in v1)."
                    ),
                }
            },
        },
        "totals": {
            "cases": str(round_qty(total_cases)),
            "base_cogs": str(money(base_total)),
            "cogs": str(money(total_per_case * total_cases)),
        },
        "shortfall": check_shortfall_many(
            client, [(line["sku"], line["qty_cases"]) for line in lines]
        ),
    }


class DeliveryShortfall(Exception):
    """Raised when stock is insufficient and force=False (spec 4.2 step 4)."""

    def __init__(self, shortages):
        self.shortages = shortages
        super().__init__(
            "Insufficient stock: "
            + "; ".join(
                f"{s['material']} short by {s['short_by']} {s['unit']}"
                for s in shortages
            )
        )


def timezone_now_date():
    from django.utils import timezone

    return timezone.now().date()


@transaction.atomic
def create_delivery(
    client,
    sku,
    qty_cases: Decimal,
    selling_price_per_case=None,
    delivery_date=None,
    note: str = "",
    force: bool = False,
):
    """
    Orchestration for the Add-Delivery flow (spec 4.2 / 3.8):
      1. Default selling price from ClientSKUPrice when not overridden.
      2. FIFO sufficiency check — if short and not force, raise
         DeliveryShortfall (API 409 -> Proceed/Cancel banner).
      3. Compute per-case COGS via actual FIFO consumption.
      4. Save delivery — the snapshot columns record the figures as at
         creation for the AUDIT trail (displayed costs stay dynamic).
      5. Create linked ClientLedgerEntry (DELIVERY, +qty*price).

    Returns (delivery, result_dict). Atomic.
    """
    from .models import ClientSKUPrice, StockDelivery

    if delivery_date is None:
        delivery_date = timezone_now_date()

    if selling_price_per_case is None:
        csp = ClientSKUPrice.objects.filter(client=client, sku=sku).first()
        if csp is None:
            raise ValueError(
                "No selling price configured for this client/SKU — "
                "provide selling_price_per_case explicitly."
            )
        selling_price_per_case = csp.selling_price_per_case

    shortfall = check_shortfall(sku, client, qty_cases)
    if shortfall and not force:
        raise DeliveryShortfall(shortfall)

    result = estimate_cogs(sku, client, qty_cases, delivery_date, preview=False)
    short_flag = bool(shortfall) or result["has_unrecorded_shortfall"]

    delivery = StockDelivery.objects.create(
        client=client,
        sku=sku,
        date=delivery_date,
        qty_cases=qty_cases,
        selling_price_per_case=selling_price_per_case,
        # Creation-time direct cost (materials consumed + print) — recorded
        # for the audit trail; every later display recomputes dynamically.
        base_cogs_per_case_snapshot=Decimal(result["per_case_base"]),
        # … and the full figure as at creation (also audit-only).
        cogs_per_case_snapshot=Decimal(result["per_case"]),
        stock_shortfall_flag=short_flag,
    )

    amount = money(Decimal(qty_cases) * selling_price_per_case)
    ClientLedgerEntry.objects.create(
        client=client,
        entry_type="DELIVERY",
        amount=amount,
        related_delivery=delivery,
        note=note or f"Delivery: {qty_cases} x {sku.description}",
        date=delivery_date,
    )

    result["delivery_id"] = delivery.id
    result["client_pending_amount"] = str(client.pending_amount)
    result["total_amount"] = str(amount)
    # Figures as at creation (so the month's case count includes this one):
    # the actual cost just consumed + the month's live overhead. From here on
    # every screen recomputes costs dynamically (dynamic_costs_for).
    per_case_current = money(
        delivery.base_cogs_per_case_snapshot + delivery.overhead_per_case_current
    )
    result["per_case_current"] = str(per_case_current)
    result["base_per_case"] = str(delivery.base_cogs_per_case_snapshot)
    result["overhead_per_case_current"] = str(delivery.overhead_per_case_current)
    result["total_cogs_current"] = str(money(delivery.qty_cases * per_case_current))
    return delivery, result


@transaction.atomic
def create_deliveries(
    client, lines, delivery_date=None, note: str = "", force: bool = False
):
    """
    Multi-SKU delivery (spec 4.2): one client, one date, one note — saved as
    one StockDelivery row per SKU line, so FIFO consumption, the creation-time
    COGS snapshot (audit only) and the linked client-ledger entry behave
    exactly as they do for a single-SKU delivery (the ledger shows one entry
    per SKU, as before).

    `lines` = [{"sku": sku, "qty_cases": Decimal,
                "selling_price_per_case": Decimal | None}, ...]

    Atomic: the aggregated stock check runs BEFORE anything is written, and a
    failure on any line rolls the whole delivery back — never half-saved.

    Returns (deliveries, result_dict). Raises DeliveryShortfall (-> 409) or
    ValueError (-> 400).
    """
    from .models import ClientSKUPrice

    if delivery_date is None:
        delivery_date = timezone_now_date()
    if not lines:
        raise ValueError("At least one SKU line is required.")

    # 1. Normalise: one row per SKU, price defaulted from the client's own
    #    ClientSKUPrice, duplicates rejected (an aggregated stock check would
    #    double-count them and the ledger would be ambiguous).
    normalised = []
    seen_skus = set()
    for line in lines:
        sku = line["sku"]
        try:
            qty_cases = Decimal(str(line["qty_cases"]))
        except Exception:
            raise ValueError(f"Quantity is required for {sku.description}.")
        if qty_cases <= 0:
            raise ValueError(
                f"Quantity must be greater than zero ({sku.description})."
            )
        if sku.id in seen_skus:
            raise ValueError(
                f"{sku.description} is listed twice — combine it into one line."
            )
        seen_skus.add(sku.id)

        price = line.get("selling_price_per_case")
        if price is None or price == "":
            csp = ClientSKUPrice.objects.filter(client=client, sku=sku).first()
            if csp is None:
                raise ValueError(
                    f"No selling price configured for {sku.description} — "
                    "enter a price."
                )
            price = csp.selling_price_per_case
        price = Decimal(str(price))
        if price < 0:
            raise ValueError(
                f"Selling price cannot be negative ({sku.description})."
            )
        normalised.append(
            {"sku": sku, "qty_cases": qty_cases, "selling_price_per_case": price}
        )

    # 2. ONE stock check for the whole delivery: materials shared by several
    #    lines are summed, so a delivery that fits overall is not blocked
    #    line by line (409 -> Proceed/Cancel, spec 4.2 step 4).
    shortfall = check_shortfall_many(
        client, [(l["sku"], l["qty_cases"]) for l in normalised]
    )
    if shortfall and not force:
        raise DeliveryShortfall(shortfall)
    # 3. Create the lines in order through the single-line path.
    created = []
    for line in normalised:
        delivery, _line_result = create_delivery(
            client,
            line["sku"],
            line["qty_cases"],
            selling_price_per_case=line["selling_price_per_case"],
            delivery_date=delivery_date,
            note=note,
            force=force,
        )
        created.append((delivery, line))

    # 4. Live figures are read only AFTER every line exists, so the delivery
    #    month's overhead is split over the delivery's final case count.
    rows = []
    total_cases = ZERO
    total_amount = ZERO
    total_cogs = ZERO
    base_total = ZERO
    for delivery, _line in created:
        amount = money(delivery.qty_cases * delivery.selling_price_per_case)
        base_total += delivery.base_cogs_per_case_snapshot * delivery.qty_cases
        total_cases += delivery.qty_cases
        total_amount += amount
        # As-at-creation figures (actual consumption + live month overhead);
        # every later display recomputes costs dynamically.
        per_case_current = money(
            delivery.base_cogs_per_case_snapshot + delivery.overhead_per_case_current
        )
        total_cogs += delivery.qty_cases * per_case_current
        rows.append(
            {
                "delivery_id": delivery.id,
                "sku": delivery.sku_id,
                "sku_description": delivery.sku.description,
                "qty_cases": str(round_qty(delivery.qty_cases)),
                "selling_price_per_case": str(money(delivery.selling_price_per_case)),
                "amount": str(amount),
                "base_cogs_per_case": str(money(delivery.base_cogs_per_case_snapshot)),
                "overhead_per_case": str(money(delivery.overhead_per_case_current)),
                "cogs_per_case": str(per_case_current),
                "total_cogs": str(money(delivery.qty_cases * per_case_current)),
                "stock_shortfall_flag": delivery.stock_shortfall_flag,
            }
        )

    month = month_key(delivery_date)
    base_per_case = base_total / total_cases if total_cases > 0 else ZERO
    oh_per_case = overhead_per_case(month)
    return [delivery for delivery, _line in created], {
        "month": month,
        "lines": rows,
        "cogs": {
            "per_case": str(money(base_per_case + oh_per_case)),
            "base_per_case": str(money(base_per_case)),
            "overhead_per_case": str(money(oh_per_case)),
        },
        "total_cases": str(round_qty(total_cases)),
        "total_amount": str(money(total_amount)),
        "total_cogs_current": str(money(total_cogs)),
        "client_pending_amount": str(client.pending_amount),
        "stock_shortfall_flag": any(
            delivery.stock_shortfall_flag for delivery, _line in created
        ),
    }


def apply_stock_adjustment(
    material, signed_quantity: Decimal, reason: str, adjustment_date=None
):
    """
    Spec 4.4 — reconciliation path (quantities in the material's unit,
    i.e. cases). Does NOT silently edit existing batches:
    - Positive qty -> new zero-price MaterialBatch (enters FIFO queue).
    - Negative qty -> consumes oldest batches via the same FIFO walker.
    Always records a StockAdjustment row with the mandatory reason.
    """
    from .models import StockAdjustment

    if adjustment_date is None:
        adjustment_date = timezone_now_date()
    if not str(reason).strip():
        raise ValueError("A reason is required for stock adjustments.")

    signed_quantity = Decimal(signed_quantity)
    references = []
    unrecorded = ZERO

    if signed_quantity > 0:
        batch = MaterialBatch.objects.create(
            material=material,
            quantity_received=signed_quantity,
            quantity_remaining=signed_quantity,
            price_per_unit=ZERO,
            arrival_date=adjustment_date,
            note=f"Stock adjustment: {reason}",
        )
        references.append({"batch_id": batch.id, "qty": str(signed_quantity)})
    elif signed_quantity < 0:
        consumption, _cost, unrecorded = consume_fifo(
            material,
            abs(signed_quantity),
            allow_negative=True,
            deficit_date=adjustment_date,
        )
        references = consumption

    adjustment = StockAdjustment.objects.create(
        material=material,
        quantity=signed_quantity,
        reason=reason,
        date=adjustment_date,
        reference_batches=references,
    )
    return adjustment, unrecorded




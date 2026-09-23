"""
FIFO batch costing engine + COGS calculation — ALL PER CASE (spec Sections
5 & 6, updated per user instruction: bottles are never considered anywhere;
every quantity, price and cost is expressed per case).

Key rules:
- Cost follows the physical batch consumed, NOT the calendar month: the cost
  used for a delivery is the weighted cost of the specific oldest batch(es)
  actually consumed. September deliveries can still carry "August pricing"
  while August stock remains in the queue.
- StockDelivery.base_cogs_per_case_snapshot freezes the DIRECT cost
  (materials consumed + print/label) at creation; it never changes.
  The OVERHEAD part is DYNAMIC (user request): the displayed per-case COGS is
  base + the delivery month's CURRENT overhead allocation, so later
  overhead/labour edits for that month flow through to every delivery of it.
  cogs_per_case_snapshot keeps the full figure as at creation (audit only).
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
    shortages = []
    qty_cases = Decimal(qty_cases)
    for material, per_case in resolve_requirements(sku, client):
        required = round_qty(qty_cases * per_case)
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


def consume_fifo(material, units: Decimal, allow_negative: bool = True):
    """
    Consume `units` (cases) from `material`'s queue, oldest batch first.
    Rolls over to the next-oldest batch when the current one is
    insufficient (spec 5).

    Returns (consumption_list, total_cost, unrecorded_shortfall):
      consumption_list: [{"batch_id", "qty", "price_per_unit"} ...]
      total_cost: weighted cost of the batch(es) actually consumed — the
        cost that feeds the delivery's COGS snapshot.
      unrecorded_shortfall: units that could NOT be booked against any batch
        (only happens when the material has zero batches at all).

    When allow_negative=True (delivery flow, spec 4.2 step 4) and all
    batches are exhausted, the remaining deficit is pushed onto the oldest
    batches, driving their quantity_remaining negative, so a later stock
    adjustment can reconcile it. Such deliveries are flagged
    stock_shortfall_flag=True. The StockAdjustment model is the only other
    writer path — batches are never silently edited elsewhere.
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
        elif allow_negative and not batches:
            # No batch rows at all — nothing to drive negative. Record the
            # shortfall; the caller flags the delivery for later adjustment.
            unrecorded_shortfall = round_qty(remaining)
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
                material, round_qty(required), allow_negative=True
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

    # Base (direct) cost per case = materials + print. This is the part that
    # gets frozen on the delivery; overhead is added dynamically.
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
      4. Save delivery with frozen cogs_per_case_snapshot.
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
        # Frozen direct cost (materials consumed + print) …
        base_cogs_per_case_snapshot=Decimal(result["per_case_base"]),
        # … and the full figure as at creation, kept for the audit trail
        # (the live total = base + the delivery month's CURRENT overhead).
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
    # Live values AFTER saving (so the month's case count includes this one).
    result["per_case_current"] = str(delivery.cogs_per_case_current)
    result["base_per_case"] = str(delivery.base_cogs_per_case_snapshot)
    result["overhead_per_case_current"] = str(delivery.overhead_per_case_current)
    result["total_cogs_current"] = str(delivery.total_cogs)
    return delivery, result


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
            material, abs(signed_quantity), allow_negative=True
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




"""
Legacy stock backfill (user-approved one-off fix-up tool).

A delivery recorded without enough stock must drive `stock_in_hand` NEGATIVE
(see cogs.consume_fifo). That rule only books the excess on BOTH paths since
the negative-stock change; deliveries recorded BEFORE it could leave their
shortfall UNRECORDED:

  * the queue existed but had nothing positive left — the excess was dropped,
    so stock stopped at 0 instead of showing the true negative figure, and
  * the material had no batches at all — nothing was booked at all
    (unrecorded_shortfall, flagged stock_shortfall_flag only).

`backfill_negative_stock` finds those legacy gaps and books them into the batch
ledger exactly the way cogs.consume_fifo books a deficit today:

  * oldest LIVE batch pushed further negative, or
  * a zero-received "deficit carrier" batch (master display price, excluded
    from the inward list) when the material has no batches.

READ-ONLY by default: it prints what it WOULD book and writes nothing. Pass
--apply to book.

Gap per material = (demand recorded on live deliveries + live negative
adjustments) - (consumption already booked in the batch ledger, i.e.
sum(quantity_received - quantity_remaining) over every batch row). A healthy
ledger yields 0 everywhere, and --apply is idempotent: booking the gap raises
"already booked" by exactly the gap, so a second run reports nothing.

CAUTION (by design): a hand-edited batch (Home keyed edit) or an upward
reconciliation also moves "already booked", so a material the user already
reconciled by hand can still show a gap. Review the dry-run report before
running with --apply.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum

from core import cogs
from core.models import (
    Material,
    MaterialBatch,
    StockAdjustment,
    StockDelivery,
    dstr,
)

ZERO = Decimal("0")

CARRIER_NOTE = (
    "Stock shortfall backfill (legacy unrecorded shortfall) — reconcile via a "
    "Stock Adjustment or a backdated stock arrival."
)


class Command(BaseCommand):
    help = (
        "Report legacy unrecorded stock shortfalls (dry run, default) and, "
        "with --apply, book them into the batch ledger so stock in hand goes "
        "negative exactly like a forced delivery does today."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the missing shortfall bookings (default: report only).",
        )

    # ------------------------------------------------------------------ data
    def _demand_by_material(self):
        """
        Expected consumption per material, from live (non soft-deleted)
        records only — the same quantities FIFO consumption would take:
          * deliveries: qty_cases x qty_per_case, resolved per client (a
            generic label resolves to that client's own label material),
            rounded per line exactly like consume_fifo,
          * negative stock adjustments: |quantity|.
        Also returns the latest contributing event date per material, used to
        date a deficit carrier batch.
        """
        demand = {}
        last_event = {}

        deliveries = (
            StockDelivery.objects.filter(is_deleted=False)
            .select_related("sku", "client")
            .order_by("date", "id")
        )
        for delivery in deliveries:
            for material, per_case_qty in cogs.resolve_requirements(
                delivery.sku, delivery.client
            ):
                required = cogs.round_qty(delivery.qty_cases * per_case_qty)
                demand[material.id] = demand.get(material.id, ZERO) + required
                if material.id not in last_event or delivery.date > last_event[
                    material.id
                ]:
                    last_event[material.id] = delivery.date

        adjustments = StockAdjustment.objects.filter(
            is_deleted=False, quantity__lt=0
        ).select_related("material")
        for adjustment in adjustments:
            material_id = adjustment.material_id
            demand[material_id] = demand.get(material_id, ZERO) + cogs.round_qty(
                abs(adjustment.quantity)
            )
            if material_id not in last_event or adjustment.date > last_event[
                material_id
            ]:
                last_event[material_id] = adjustment.date

        return demand, last_event


    def _booked_consumption(self, material):
        """
        Consumption already recorded in this material's batch ledger:
        sum(quantity_received - quantity_remaining) over EVERY batch row
        (soft-deleted rows included — deleting a wrongly keyed arrival must
        not erase the consumption that was booked against it).
        """
        totals = material.batches.aggregate(
            received=Sum("quantity_received"), remaining=Sum("quantity_remaining")
        )
        received = totals["received"] or ZERO
        remaining = totals["remaining"] or ZERO
        return cogs.round_qty(received - remaining)

    # -------------------------------------------------------------- booking
    def _target_batch(self, material):
        """Same pick as consume_fifo's deficit path: the oldest live batch."""
        return cogs.fifo_batches_for(material).first()

    def _target_description(self, material):
        batch = self._target_batch(material)
        if batch is not None:
            return (
                f"push the oldest batch #{batch.id} "
                f"(arrived {batch.arrival_date}) further negative"
            )
        return "create a zero-received deficit carrier batch"

    @transaction.atomic
    def _book(self, material, gap, event_date):
        """Book `gap` on the ledger exactly like a forced delivery does."""
        batch = self._target_batch(material)
        if batch is not None:
            batch.quantity_remaining = cogs.round_qty(batch.quantity_remaining - gap)
            batch.save(skip_audit=True)
            return f"batch #{batch.id} pushed to {dstr(batch.quantity_remaining)}"

        carrier = MaterialBatch.objects.create(
            material=material,
            quantity_received=ZERO,
            quantity_remaining=cogs.round_qty(-gap),
            price_per_unit=material.current_price_per_unit,
            arrival_date=event_date or cogs.timezone_now_date(),
            note=CARRIER_NOTE,
        )
        return (
            f"deficit carrier batch #{carrier.id} at "
            f"{dstr(carrier.quantity_remaining)}"
        )

    # --------------------------------------------------------------- driver
    def handle(self, *args, **options):
        apply_changes = options["apply"]
        demand_by_material, last_event_by_material = self._demand_by_material()

        gaps = []
        for material in Material.objects.all().order_by("name"):
            demand = demand_by_material.get(material.id, ZERO)
            booked = self._booked_consumption(material)
            gap = cogs.round_qty(demand - booked)
            if gap > ZERO:
                gaps.append(
                    (
                        material,
                        demand,
                        booked,
                        gap,
                        last_event_by_material.get(material.id),
                    )
                )

        if not gaps:
            self.stdout.write(
                self.style.SUCCESS(
                    "No unrecorded stock shortfalls found — the batch ledger "
                    "reconciles with every live delivery and adjustment."
                )
            )
            return

        mode = "APPLY" if apply_changes else "DRY RUN"
        self.stdout.write(
            self.style.MIGRATE_HEADING(f"[{mode}] legacy shortfall backfill")
        )

        for material, demand, booked, gap, event_date in gaps:
            self.stdout.write(f"{material.name} ({material.unit_of_measure}):")
            self.stdout.write(
                f"  demand {dstr(demand)} — already booked {dstr(booked)} "
                f"— unrecorded shortfall {dstr(gap)}"
            )
            if apply_changes:
                self.stdout.write(
                    f"  booked -> {self._book(material, gap, event_date)}"
                )
            else:
                self.stdout.write(f"  would {self._target_description(material)}")

        if apply_changes:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Booked {len(gaps)} legacy shortfall(s) into the batch "
                    "ledger. Re-run to confirm the report is clean."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN — nothing was written ({len(gaps)} material(s) "
                    "with a gap). Re-run with --apply to book these shortfalls."
                )
            )

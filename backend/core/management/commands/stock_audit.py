"""
Read-only stock reconciliation report (user bug: "in-hand stock does not match
the batch ledger").

WHY THIS EXISTS

Stock in hand is never a stored figure: it is sum(quantity_remaining) over the
live batch rows, so it can only drift from reality if a batch row drifts. Two
kinds of drift are worth looking for on real data:

  * a row that was over-restored — `quantity_remaining` above
    `quantity_received`. Deleting a delivery used to run the FIFO rollback
    twice (fixed in cogs.void_delivery), which handed the same cases back more
    than once and left exactly this shape behind.
  * a row whose bookings disagree with the deliveries recorded against the
    material — the per-material gap below.

Writes nothing, ever: it is a report to run against production before deciding
on a repair (`backfill_negative_stock --apply` for genuine unrecorded
shortfalls, or a Stock Adjustment for cases that never existed).

Run:  python manage.py stock_audit
      python manage.py stock_audit --voided 50   # more deleted deliveries
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import F, Sum

from core import cogs
from core.models import (
    Material,
    MaterialBatch,
    StockAdjustment,
    StockDelivery,
    dstr,
    qty,
)

ZERO = Decimal("0")


class Command(BaseCommand):
    help = (
        "Report stock-in-hand against the batch ledger: over-restored rows, "
        "negative rows, deleted rows that still hold stock, per-material gaps "
        "and deliveries whose rollback could never run. Read-only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--voided",
            type=int,
            default=20,
            help="How many deleted deliveries to list (default 20).",
        )
        parser.add_argument(
            "--no-log",
            type=int,
            default=20,
            help=(
                "How many live deliveries with an empty consumption log to "
                "list (default 20)."
            ),
        )
        parser.add_argument(
            "--bookings",
            type=int,
            default=5,
            help=(
                "How many orphan delivery bookings to list per material "
                "(default 5)."
            ),
        )

    def _totals(self):
        live = MaterialBatch.objects.filter(is_deleted=False)
        every = MaterialBatch.objects.all()
        self.stdout.write(
            f"batches: {live.count()} live / {every.count()} total"
        )
        for label, qs in (("live", live), ("all ", every)):
            received = qs.aggregate(s=Sum("quantity_received"))["s"] or ZERO
            remaining = qs.aggregate(s=Sum("quantity_remaining"))["s"] or ZERO
            self.stdout.write(
                f"{label} received {dstr(received)}  remaining {dstr(remaining)}"
            )
        self.stdout.write(
            "deliveries: %s live / %s deleted"
            % (
                StockDelivery.objects.filter(is_deleted=False).count(),
                StockDelivery.objects.filter(is_deleted=True).count(),
            )
        )

    def _anomalies(self):
        over = (
            MaterialBatch.objects.filter(
                is_deleted=False, quantity_remaining__gt=F("quantity_received")
            )
            .select_related("material")
            .order_by("material__name", "arrival_date")
        )
        self.stdout.write("\nOver-restored rows (remaining > received):")
        if not over:
            self.stdout.write("  none")
        for batch in over:
            self.stdout.write(
                f"  batch #{batch.id} {batch.material.name}: "
                f"received {dstr(batch.quantity_received)} "
                f"remaining {dstr(batch.quantity_remaining)} "
                f"over {dstr(batch.quantity_remaining - batch.quantity_received)}"
            )

        negative = (
            MaterialBatch.objects.filter(is_deleted=False, quantity_remaining__lt=0)
            .select_related("material")
            .order_by("material__name", "arrival_date")
        )
        self.stdout.write("\nNegative rows (delivered before the stock arrived):")
        if not negative:
            self.stdout.write("  none")
        for batch in negative:
            self.stdout.write(
                f"  batch #{batch.id} {batch.material.name} arrived "
                f"{batch.arrival_date}: received {dstr(batch.quantity_received)} "
                f"remaining {dstr(batch.quantity_remaining)}"
            )

        ghost = (
            MaterialBatch.objects.filter(is_deleted=True)
            .exclude(quantity_remaining=0)
            .select_related("material")
            .order_by("material__name", "arrival_date")
        )
        self.stdout.write("\nDeleted rows that still carry a balance:")
        if not ghost:
            self.stdout.write("  none")
        for batch in ghost:
            # A row deleted while it still held stock took that stock out of the
            # balance at the moment of deletion. When the deletion recorded a
            # reason (soft-delete paths write one), show it: an intentional
            # removal such as a duplicate re-key then reads as an audit trail
            # instead of an unexplained ghost.
            reason = ""
            for entry in reversed(batch.edit_history or []):
                if entry.get("reason"):
                    reason = f" — {entry['reason']}"
                    break
            self.stdout.write(
                f"  batch #{batch.id} {batch.material.name}: "
                f"remaining {dstr(batch.quantity_remaining)}{reason}"
            )

    def _per_material(self):
        """
        expected consumption (live deliveries + negative adjustments) against
        what the ledger has already absorbed. A non-zero gap means the two
        disagree — the batch rows were edited by hand, or a rollback ran twice.
        """
        demand, _ = cogs.legacy_demand_by_material()
        self.stdout.write("\nPer-material reconciliation (all time):")
        self.stdout.write(
            "  %-30s %10s %10s %10s %10s %10s"
            % ("material", "received", "remaining", "booked", "expected", "gap")
        )
        gaps = 0
        for material in Material.objects.all().order_by("name"):
            batches = list(MaterialBatch.objects.filter(material=material))
            if not batches:
                continue
            received = sum((b.quantity_received for b in batches), ZERO)
            remaining = sum((b.quantity_remaining for b in batches), ZERO)
            live_remaining = sum(
                (b.quantity_remaining for b in batches if not b.is_deleted), ZERO
            )
            booked = received - remaining
            expected = demand.get(material.id, ZERO)
            gap = cogs.round_qty(expected - booked)
            if gap:
                gaps += 1
            self.stdout.write(
                "  %-30s %10s %10s %10s %10s %10s"
                % (
                    material.name[:30],
                    dstr(received),
                    dstr(live_remaining),
                    dstr(booked),
                    dstr(expected),
                    dstr(gap),
                )
            )
        if not gaps:
            self.stdout.write(
                self.style.SUCCESS(
                    "  every material reconciles: booked consumption matches the "
                    "deliveries and adjustments on record."
                )
            )

    def _bookings(self, limit):
        """
        Delivery bookings against the arrival rows they actually name.

        The per-material table above cannot see two failure shapes, because both
        leave `received - remaining` equal to total demand:

        * ORPHAN BOOKING — a delivery's consumption_log names a batch id that is
          now soft-deleted (or gone). Those cases really left the shelf, but the
          row that absorbed them is no longer in the register, so a replacement
          row keyed afterwards — typically backdated to the same arrival date —
          still carries its full balance and stock reads high by exactly that
          amount. Deleting a row that was already consumed is stock-neutral by
          itself; re-keying the same arrival after it is what double counts.
        * UNLOGGED CONSUMPTION — a live row whose own received - remaining is not
          backed by any delivery booking or adjustment: units the audit trail
          cannot attribute (rows written before consumption_log existed, or a
          hand edit of quantity_received).
        """
        live_ids = set(
            MaterialBatch.objects.filter(is_deleted=False).values_list("id", flat=True)
        )
        batches = {
            b.id: b
            for b in MaterialBatch.objects.all().select_related("material")
        }
        material_names = dict(Material.objects.values_list("id", "name"))

        booked = {}
        orphans = []
        for delivery in StockDelivery.objects.filter(is_deleted=False).order_by(
            "date", "id"
        ):
            for block in delivery.consumption_log or []:
                material_id = block.get("material_id")
                for entry in block.get("consumed") or []:
                    batch_id = entry.get("batch_id")
                    amount = qty(entry.get("qty") or 0)
                    key = (material_id, batch_id)
                    booked[key] = booked.get(key, ZERO) + amount
                    if batch_id not in live_ids:
                        orphans.append((delivery, material_id, batch_id, amount))

        adjusted = {}
        for adjustment in StockAdjustment.objects.filter(is_deleted=False):
            for entry in adjustment.reference_batches or []:
                key = (adjustment.material_id, entry.get("batch_id"))
                adjusted[key] = adjusted.get(key, ZERO) + qty(entry.get("qty") or 0)

        self.stdout.write(
            "\nBookings pointing at an arrival row that is no longer live:"
        )
        if not orphans:
            self.stdout.write("  none")
        totals = {}
        for _delivery, material_id, _batch_id, amount in orphans:
            totals[material_id] = totals.get(material_id, ZERO) + amount
        for material_id in sorted(totals, key=lambda m: -totals[m]):
            rows = [o for o in orphans if o[1] == material_id]
            self.stdout.write(
                "  %s: %s units across %s deliveries"
                % (material_names.get(material_id, material_id), dstr(totals[material_id]), len(rows))
            )
            for delivery, _mid, batch_id, amount in rows[:limit]:
                state = "deleted row" if batch_id in batches else "row missing"
                self.stdout.write(
                    f"    delivery #{delivery.id} {delivery.date} -> batch "
                    f"#{batch_id} ({state}) {dstr(amount)}"
                )
        if orphans:
            self.stdout.write(
                "  ^ these units were taken out of rows the register no longer "
                "shows: any backdated replacement row for the same arrival still "
                "reports its full balance, so stock is overstated by that amount."
            )

        self.stdout.write("\nLive rows whose consumption no booking explains:")
        unbacked = 0
        for batch in sorted(
            (b for b in batches.values() if not b.is_deleted),
            key=lambda b: (b.material.name, b.arrival_date, b.id),
        ):
            consumed = qty(batch.quantity_received - batch.quantity_remaining)
            explained = booked.get((batch.material_id, batch.id), ZERO) + adjusted.get(
                (batch.material_id, batch.id), ZERO
            )
            gap = cogs.round_qty(consumed - explained)
            if not gap:
                continue
            unbacked += 1
            self.stdout.write(
                f"  batch #{batch.id} {batch.material.name} arrived "
                f"{batch.arrival_date}: received {dstr(batch.quantity_received)} "
                f"remaining {dstr(batch.quantity_remaining)} consumed "
                f"{dstr(consumed)} booked {dstr(explained)} unexplained {dstr(gap)}"
            )
        if not unbacked:
            self.stdout.write(self.style.SUCCESS("  none"))

        self.stdout.write(
            "\nArrival rows deleted after stock had been taken out of them:"
        )
        stripped = [
            b
            for b in batches.values()
            if b.is_deleted and b.quantity_received > b.quantity_remaining
        ]
        if not stripped:
            self.stdout.write("  none")
        for batch in sorted(stripped, key=lambda b: (b.arrival_date, b.id)):
            deletions = [
                h for h in (batch.edit_history or []) if h.get("action") == "deleted"
            ]
            when = deletions[-1].get("timestamp", "")[:19] if deletions else "unknown"
            self.stdout.write(
                f"  batch #{batch.id} {batch.material.name} arrived "
                f"{batch.arrival_date}: received {dstr(batch.quantity_received)} "
                f"consumed "
                f"{dstr(batch.quantity_received - batch.quantity_remaining)} "
                f"deleted {when}"
            )

    def _deliveries(self, voided_limit, no_log_limit):
        live = StockDelivery.objects.filter(is_deleted=False)
        unlogged = live.filter(consumption_log=[]).order_by("date", "id")
        self.stdout.write("\nLive deliveries:")
        self.stdout.write(
            f"  with an empty consumption log: {unlogged.count()} of {live.count()}"
        )
        self.stdout.write(
            "  (recorded before the log existed — deleting one reports "
            "rollback_skipped and cannot hand its stock back)"
        )
        for delivery in unlogged[:no_log_limit]:
            self.stdout.write(
                f"    #{delivery.id} {delivery.date} qty {dstr(delivery.qty_cases)}"
            )

        self.stdout.write(f"\nDeleted deliveries (latest {voided_limit}):")
        rows = StockDelivery.objects.filter(is_deleted=True).order_by("-date", "-id")[
            :voided_limit
        ]
        if not rows:
            self.stdout.write("  none")
        for delivery in rows:
            history = delivery.edit_history or []
            deletions = [h for h in history if h.get("action") == "deleted"]
            skipped = deletions[-1].get("stock_rollback_skipped") if deletions else None
            self.stdout.write(
                f"  #{delivery.id} {delivery.date} qty {dstr(delivery.qty_cases)} "
                f"log_lines {len(delivery.consumption_log or [])} "
                f"rollback_skipped {skipped}"
            )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("stock audit (read-only)"))
        self._totals()
        self._anomalies()
        self._per_material()
        self._bookings(options["bookings"])
        self._deliveries(options["voided"], options["no_log"])
        self.stdout.write(
            self.style.WARNING(
                "\nNothing was written. Repair guidance: a genuine unrecorded "
                "shortfall -> backfill_negative_stock --apply; stock that never "
                "existed -> Stock Adjustment with a reason; bookings stranded on "
                "a deleted arrival row -> reconcile_rekeyed_arrivals (restores "
                "the consumed original, deletes the duplicate re-key, and takes "
                "exactly the phantom quantity out of stock; see the section "
                "above for the ids)."
            )
        )

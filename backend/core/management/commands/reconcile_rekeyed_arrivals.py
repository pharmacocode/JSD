"""
Reconcile arrivals that were re-keyed after being deleted.

WHY THIS EXISTS

Deleting a batch that has already been fully consumed is STOCK-NEUTRAL
(remaining is 0, so in-hand does not move), which makes it look harmless. But
the deliveries that consumed it keep the original batch id in their
`consumption_log`, so the consumption stays attached to a row the register no
longer shows. Re-keying "the same arrival" afterwards — usually backdated to the
original arrival date — then ADDS its quantity back on top of consumption that
already happened.

The result is the shape reported from production ("in-hand stock is not actually
there"):

  * `stock_audit` says every material reconciles (booked == expected), because
    received - remaining still equals total demand, and
  * in-hand is overstated by exactly the quantity that was re-keyed, and
  * `carryForward` on the Home screen back-solves an impossible opening balance.

THE REPAIR (two steps, both reversible)

  1. RESTORE the original (soft-deleted) rows. Everything they consumed stays
     consumed — their remaining is 0 — so stock does not move; the stranded
     bookings re-attach to live rows and the month's arrival totals become real
     again.
  2. DELETE the duplicate re-keyed rows. Each one still carries its full
     quantity, so removing it takes exactly that inflated stock out.

READ-ONLY by default: prints the plan, the stock delta per material and the
resulting balances, and writes nothing. Pass --apply to perform it.

GUARDS (each aborts before anything is written, unless --force):

  * a restore whose `quantity_remaining` is NOT zero would add stock,
  * a delete of a row that has deliveries booked against it would strand that
    consumption,
  * an id that does not exist, is already in the requested state, or appears in
    both lists.

Example (production repair of the 2026-10-29 rebuild):

    python manage.py reconcile_rekeyed_arrivals \
        --restore 11,17,18,19,22,26,27,28 \
        --delete 36,37,38,39,40 \
        --reason "duplicate re-key of arrivals already consumed (2026-10-29)" \
        --apply
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core import cogs, perfcache
from core.models import MaterialBatch, StockDelivery, dstr, qty

ZERO = Decimal("0")


class Command(BaseCommand):
    help = (
        "Restore arrival rows that were deleted after being consumed and delete "
        "the duplicate rows re-keyed in their place. Read-only unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--restore",
            default="",
            help="Comma-separated batch ids to restore (must be soft-deleted).",
        )
        parser.add_argument(
            "--delete",
            default="",
            help="Comma-separated batch ids to soft-delete (the duplicate re-keys).",
        )
        parser.add_argument(
            "--reason",
            default="duplicate re-key of an arrival already consumed",
            help="Reason written into each row's edit_history.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform the repair. Without it nothing is written.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow a restore that moves stock or a delete that strands bookings.",
        )

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _ids(raw):
        ids = []
        for part in str(raw or "").replace(" ", "").split(","):
            if not part:
                continue
            if not part.isdigit():
                raise CommandError(f"not a batch id: {part!r}")
            ids.append(int(part))
        return ids

    @staticmethod
    def _bookings():
        """{batch_id: qty} booked out of each batch by live deliveries."""
        booked = {}
        for delivery in StockDelivery.objects.filter(is_deleted=False):
            for block in delivery.consumption_log or []:
                for entry in block.get("consumed") or []:
                    batch_id = entry.get("batch_id")
                    booked[batch_id] = booked.get(batch_id, ZERO) + qty(
                        entry.get("qty") or 0
                    )
        return booked

    @staticmethod
    def _sdelta(delta):
        """+5 / -200 / 0 — a signed quantity for the report."""
        if delta > ZERO:
            return f"+{dstr(delta)}"
        return dstr(delta)

    @staticmethod
    def _mark(batch, action, reason):
        """Audit entry in the same shape AuditModel.soft_delete writes."""
        history = list(batch.edit_history or [])
        history.append(
            {
                "timestamp": timezone.now().isoformat(),
                "action": action,
                "reason": reason,
                "before": batch._audit_snapshot(),
            }
        )
        batch.edit_history = history

    def _report(self, plan, before, reason):
        self.stdout.write(self.style.MIGRATE_HEADING("re-keyed arrival reconcile"))
        self.stdout.write(f"reason: {reason}")
        if not plan:
            self.stdout.write("  no changes to make")
            return
        self.stdout.write(
            "  %-8s %-8s %-26s %10s %10s %11s"
            % ("action", "batch", "material", "arrival", "received", "stock +/-")
        )
        for batch, action, delta in plan:
            self.stdout.write(
                "  %-8s #%-7s %-26s %10s %10s %11s"
                % (
                    action,
                    batch.id,
                    batch.material.name[:26],
                    batch.arrival_date,
                    dstr(batch.quantity_received),
                    self._sdelta(delta),
                )
            )
        deltas = {}
        names = {}
        for batch, _action, delta in plan:
            deltas[batch.material_id] = deltas.get(batch.material_id, ZERO) + delta
            names[batch.material_id] = batch.material.name
        self.stdout.write("\n  resulting balance per material:")
        for material_id, delta in sorted(deltas.items(), key=lambda kv: names[kv[0]]):
            now = before.get(material_id, ZERO)
            self.stdout.write(
                "    %-26s %10s %11s = %10s"
                % (
                    names[material_id],
                    dstr(now),
                    self._sdelta(delta),
                    dstr(now + delta),
                )
            )

    # -- command ---------------------------------------------------------
    def handle(self, *args, **options):
        restore_ids = self._ids(options["restore"])
        delete_ids = self._ids(options["delete"])
        reason = options["reason"]
        apply_changes = options["apply"]
        force = options["force"]

        if not restore_ids and not delete_ids:
            raise CommandError("nothing to do: pass --restore and/or --delete")
        both = sorted(set(restore_ids) & set(delete_ids))
        if both:
            raise CommandError(f"batch ids in both lists: {both}")

        rows = {
            b.id: b
            for b in MaterialBatch.objects.filter(
                id__in=restore_ids + delete_ids
            ).select_related("material")
        }
        missing = sorted(set(restore_ids + delete_ids) - set(rows))
        if missing:
            raise CommandError(f"batch ids not found: {missing}")

        booked = self._bookings()
        before = cogs.stock_map()
        plan = []  # (batch, action, stock delta)
        problems = []

        for batch_id in restore_ids:
            batch = rows[batch_id]
            if not batch.is_deleted:
                problems.append(
                    f"batch #{batch_id} is already live — nothing to restore"
                )
                continue
            delta = qty(batch.quantity_remaining)
            if delta != ZERO and not force:
                problems.append(
                    f"batch #{batch_id} still carries {dstr(delta)} — restoring it "
                    "would ADD stock (pass --force if that is intended)"
                )
                continue
            plan.append((batch, "restore", delta))

        for batch_id in delete_ids:
            batch = rows[batch_id]
            if batch.is_deleted:
                problems.append(
                    f"batch #{batch_id} is already deleted — nothing to delete"
                )
                continue
            stranded = booked.get(batch_id, ZERO)
            if stranded and not force:
                problems.append(
                    f"batch #{batch_id} has {dstr(stranded)} booked against it — "
                    "deleting it would strand that consumption (pass --force if "
                    "this row really is the duplicate)"
                )
                continue
            plan.append((batch, "delete", -qty(batch.quantity_remaining)))

        self._report(plan, before, reason)
        if problems:
            for line in problems:
                self.stderr.write(self.style.ERROR(f"  blocked: {line}"))
            raise CommandError("nothing written — fix the blocked rows first")

        if not apply_changes:
            self.stdout.write(
                self.style.WARNING(
                    "\nDry run: nothing was written. Re-run with --apply to do it."
                )
            )
            return

        with transaction.atomic():
            for batch, action, _delta in plan:
                self._mark(
                    batch, "restored" if action == "restore" else "deleted", reason
                )
                batch.is_deleted = action == "delete"
                batch.save(skip_audit=True)
        perfcache.invalidate()

        after = cogs.stock_map()
        names = {b.material_id: b.material.name for b, _a, _d in plan}
        self.stdout.write(self.style.SUCCESS("\nApplied. Stock in hand now:"))
        for material_id, name in sorted(names.items(), key=lambda kv: kv[1]):
            self.stdout.write(
                "  %-26s %10s -> %10s"
                % (
                    name,
                    dstr(before.get(material_id, ZERO)),
                    dstr(after.get(material_id, ZERO)),
                )
            )
        self.stdout.write(
            "Run `python manage.py stock_audit` to confirm no booking is left "
            "stranded on a deleted row."
        )

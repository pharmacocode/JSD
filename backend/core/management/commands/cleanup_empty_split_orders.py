# -*- coding: utf-8 -*-
'''Order-shell cleanup (user-approved one-off fix-up tool).

Before the whole-selection split fix, confirming the Ready-to-Deliver line
picker with every line ticked at FULL quantity still ran the SPLIT path:
the cases moved to a brand-new order and the original order was left behind
with ZERO items - an empty shell sitting on the board (Pending or Ready to
Deliver, 0 cases) while the real cases live on the child order. (User
report: ghost orders after splitting.)

`cleanup_empty_split_orders` finds every live order with no items that has
at least one split child - a shape the app can no longer produce (the
write serializer rejects an empty line set) - and soft-deletes the shell
exactly like the UI delete does. Nothing else is touched: the child keeps
its items, deliveries, ledger entries and status; only its `source_order`
link to the now-hidden shell is cleared, so the board never shows a
"split from #" chip pointing at an order that no longer appears anywhere.

READ-ONLY by default: it lists the shells it WOULD purge and writes
nothing. Pass --apply to purge. Idempotent - a purged shell is
soft-deleted, so a re-run reports a clean board.

Statement balances are unaffected: order rows never create ledger entries
(only deliveries do), so hiding an empty shell moves no money.
'''

from django.core.management.base import BaseCommand
from django.db.models import Count

from core.models import ClientOrder


class Command(BaseCommand):
    help = (
        "Report order shells left empty by the pre-fix whole-selection split "
        "bug (dry run, default) and, with --apply, soft-delete them without "
        "touching the split child that holds the real cases."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Soft-delete the empty shells (default: report only).",
        )

    def _shells(self):
        """
        Live orders with NO items and at least one split child (any state).
        The counts use distinct=True so the two joins cannot multiply rows.
        A legitimately saved order always has lines (the write serializer
        requires a non-empty list), so a live zero-item order with split
        children can only be a pre-fix shell.
        """
        return (
            ClientOrder.objects.filter(is_deleted=False)
            .annotate(
                n_items=Count("items", distinct=True),
                n_children=Count("split_orders", distinct=True),
            )
            .filter(n_items=0, n_children__gte=1)
            .order_by("id")
        )

    def handle(self, *args, **options):
        shells = list(self._shells())
        apply = options["apply"]

        if not shells:
            self.stdout.write("No empty split orders to clean up.")
            return

        verb = "Purging" if apply else "DRY RUN — would purge"
        for shell in shells:
            children = [str(o.id) for o in shell.split_orders.all()]
            self.stdout.write(
                f"{verb} order #{shell.id} ({shell.client.name}, "
                f"{shell.status}, 0 items) — the cases live on split "
                f"order(s) #{', #'.join(children)}."
            )
            if apply:
                shell.soft_delete()
                # Clear the live children's link so the board never shows a
                # "split from #" chip pointing at the hidden shell. Deleted
                # children keep theirs — their audit trail stays intact.
                ClientOrder.objects.filter(
                    source_order=shell, is_deleted=False
                ).update(source_order=None)

        if apply:
            self.stdout.write(f"[APPLY] purged {len(shells)} empty order shell(s).")
        else:
            self.stdout.write(
                "DRY RUN — nothing was changed; re-run with --apply to purge."
            )
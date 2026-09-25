"""
JSD Group — data models (spec Section 3).

Conventions:
- All monetary fields: DecimalField, 2 dp, INR (₹), no GST fields.
- Fractional quantity fields (e.g. 0.095 of a label): DecimalField, >= 4 dp.
- Transactional records (MaterialBatch, StockDelivery, ClientLedgerEntry) are
  never hard-deleted: `is_deleted` soft-delete + `edit_history` JSON audit log
  (spec Section 3.7).
- Client pending balance is NEVER stored on Client — it is always a live sum
  of ClientLedgerEntry amounts (single source of truth, spec Section 3.2).
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F
from django.utils import timezone

TWO_PLACES = Decimal("0.01")
FOUR_PLACES = Decimal("0.0001")


def money(value):
    """Round to 2 dp (money)."""
    return Decimal(value).quantize(TWO_PLACES)


def qty(value):
    """Round to 4 dp (fractional quantities like 0.095 labels/bottle)."""
    return Decimal(value).quantize(FOUR_PLACES)


def dstr(value):
    """Decimal -> trimmed string for JSON display (24.0000 -> '24')."""
    s = str(value)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _split_items(items, as_of):
    """
    Split dated movements around an "as of" marker date. Entries dated strictly
    AFTER the marked date are applied on top of the entered figure; everything
    on or before it is already inside that figure (superseded, never counted
    twice). `as_of=None` means no marker -> nothing is superseded.
    """
    if as_of is None:
        return list(items), []
    applied = [i for i in items if i["date"] > as_of]
    superseded = [i for i in items if i["date"] <= as_of]
    return applied, superseded


def _item_out(item):
    """Dated movement -> JSON row shared by the client/vendor marker dialogs."""
    return {
        "id": item["id"],
        "date": item["date"].isoformat(),
        "entry_type": item.get("entry_type", ""),
        "label": item["label"],
        "detail": item.get("detail", ""),
        "amount": str(money(item["amount"])),
    }


def _ledger_item(entry):
    """ClientLedgerEntry -> raw movement row (_item_out renders the JSON row)."""
    detail = ""
    if entry.related_delivery_id:
        d = entry.related_delivery
        detail = f"{d.sku.description} × {d.qty_cases} cases"
    return {
        "id": entry.id,
        "date": entry.date,
        "entry_type": entry.entry_type,
        "label": entry.note or entry.get_entry_type_display(),
        "detail": detail,
        "amount": entry.amount,
    }


def _marker_payload(as_of, base, applied_total, applied, superseded, pure):
    """
    Common shape for both marker dialogs (see BalanceMarkerDialog.vue):
    entered `amount` + the movements after `as_of` = `total`.
    """
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "marker_active": as_of is not None,
        "amount": str(money(base)),
        "total": str(money(base + applied_total)),
        "pure_total": str(money(pure)),
        "after": {
            "count": len(applied),
            "total": str(money(applied_total)),
            "entries": [_item_out(i) for i in applied],
        },
        "before": {
            "count": len(superseded),
            "total": str(money(pure - applied_total)),
            "entries": [_item_out(i) for i in superseded],
        },
    }


class AuditModel(models.Model):
    """
    Base for editable/deletable transactional records (spec Section 3.7).
    - Soft delete only (is_deleted), excluded from normal queries.
    - On edit, previous state is appended to edit_history as JSON with a
      timestamp before the new state is saved.
    - `is_edited` drives the "Edited" badge in the UI.
    """

    is_deleted = models.BooleanField(default=False)
    is_edited = models.BooleanField(default=False)
    edit_history = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def _audit_snapshot(self):
        """State to preserve in edit_history before an update. Override in subclasses."""
        return {}

    def save(self, *args, **kwargs):
        # Pop our custom flag so it never reaches Django's Model.save().
        skip_audit = kwargs.pop("skip_audit", False)
        if not skip_audit:
            self._run_audit()
        super().save(*args, **kwargs)

    def _run_audit(self):
        """Append previous state to edit_history if this is a modifying save."""
        if self._state.adding:
            return
        try:
            previous = type(self).objects.get(pk=self.pk)
        except type(self).DoesNotExist:
            return
        old_snapshot = previous._audit_snapshot()
        new_snapshot = self._audit_snapshot()
        if old_snapshot != new_snapshot:
            history = list(self.edit_history or [])
            history.append(
                {
                    "timestamp": timezone.now().isoformat(),
                    "before": old_snapshot,
                    "after": new_snapshot,
                }
            )
            self.edit_history = history
            self.is_edited = True

    def soft_delete(self):
        """Soft delete + audit entry. Never removes the row."""
        history = list(self.edit_history or [])
        history.append(
            {
                "timestamp": timezone.now().isoformat(),
                "action": "deleted",
                "before": self._audit_snapshot(),
            }
        )
        self.edit_history = history
        self.is_deleted = True
        self.save(skip_audit=True)

    def restore(self):
        self.is_deleted = False
        self.save(skip_audit=True)


# ---------------------------------------------------------------------------
# Vendor master (vendor list + contact + amount owed, selected in materials)
# ---------------------------------------------------------------------------
class Vendor(models.Model):
    name = models.CharField(max_length=200, unique=True)
    contact_number = models.CharField(max_length=30, blank=True, default="")
    # Payable marker (user request): what we owed this vendor, entered/edited at
    # any time and valid as of a date the user marks. `payable_as_of_date is
    # None` => no marker and the payable is simply purchases − payments. With a
    # marker, arrived stock + payments dated strictly AFTER that date are added
    # on top of the entered figure (older ones are already inside it).
    payable_as_of_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    payable_as_of_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def has_payable_marker(self):
        return self.payable_as_of_date is not None

    def _purchased(self, after=None):
        """Raw landed value of arrived stock, optionally only after a date."""
        from django.db.models import Sum

        qs = MaterialBatch.objects.filter(material__vendor=self, is_deleted=False)
        if after is not None:
            qs = qs.filter(arrival_date__gt=after)
        return (
            qs.aggregate(s=Sum(F("quantity_received") * F("price_per_unit")))["s"]
            or Decimal("0")
        )

    def _paid(self, after=None):
        """Raw payments made to this vendor, optionally only after a date."""
        from django.db.models import Sum

        qs = VendorPayment.objects.filter(vendor=self, is_deleted=False)
        if after is not None:
            qs = qs.filter(date__gt=after)
        return qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def total_purchased(self):
        """Landed value of all arrived stock from this vendor's materials."""
        return money(self._purchased())

    @property
    def total_paid(self):
        return money(self._paid())

    @property
    def amount_owed(self):
        """
        What we still owe (INR). Without a marker: purchases − payments over all
        time. With a marker: the user's figure as of the marked date, plus every
        purchase/payment dated strictly after it.
        """
        if self.payable_as_of_date is None:
            return money(self._purchased() - self._paid())
        return money(
            self.payable_as_of_amount
            + self._purchased(self.payable_as_of_date)
            - self._paid(self.payable_as_of_date)
        )

    def payable_items(self):
        """Every payable movement (purchases +, payments −), oldest first."""
        items = []
        for b in (
            MaterialBatch.objects.filter(material__vendor=self, is_deleted=False)
            .select_related("material")
            .order_by("arrival_date", "id")
        ):
            items.append(
                {
                    "id": b.id,
                    "date": b.arrival_date,
                    "entry_type": "PURCHASE",
                    "label": b.material.name,
                    "detail": f"{dstr(b.quantity_received)} × {b.price_per_unit}",
                    # Display row only — totals use the exact aggregates below.
                    "amount": b.quantity_received * b.price_per_unit,
                }
            )
        for p in VendorPayment.objects.filter(vendor=self, is_deleted=False):
            items.append(
                {
                    "id": p.id,
                    "date": p.date,
                    "entry_type": "PAYMENT",
                    "label": p.note or "Payment",
                    "detail": "",
                    "amount": -p.amount,
                }
            )
        items.sort(key=lambda i: (i["date"], i["id"]))
        return items

    def payable_breakdown(self, as_of=None, amount=None):
        """
        Payable split for the marker, in the shared marker-dialog payload shape
        (see Client.pending_breakdown). Pass `as_of`/`amount` to preview a
        candidate marker without saving it.
        """
        items = self.payable_items()
        applied, superseded = _split_items(items, as_of)
        pure = self._purchased() - self._paid()
        if as_of is None:
            base = Decimal("0")
            after_total = pure
        else:
            base = self.payable_as_of_amount if amount is None else amount
            after_total = self._purchased(as_of) - self._paid(as_of)
        return _marker_payload(as_of, base, after_total, applied, superseded, pure)


class VendorPayment(models.Model):
    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, related_name="payments"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField(default=timezone.now)
    note = models.TextField(blank=True, default="")
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Paid {self.vendor.name}: {self.amount}"


# ---------------------------------------------------------------------------
# 3.1 Client
# ---------------------------------------------------------------------------
class Client(models.Model):
    name = models.CharField(max_length=200, unique=True)
    # Plain URL — UI opens it in a new tab; no embedding/geocoding (spec 9).
    google_maps_url = models.URLField(blank=True, default="")
    contact_number = models.CharField(max_length=30, blank=True, default="")
    # Starting balance entered once at creation (pre-app debt).
    opening_pending_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    # Pending marker (user request): the pending figure the user enters/edits at
    # any time, valid as of a date they mark. Ledger entries dated strictly AFTER
    # that date are added on top (see pending_amount / pending_breakdown); older
    # ones are already inside the figure. `pending_as_of_date is None` => no
    # marker and the pending balance is the plain live ledger sum (spec 3.2).
    pending_as_of_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    pending_as_of_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        # One-time opening balance -> ledger entry so the live pending sum
        # works from day one (spec 3.1 / 3.2). Created exactly once, on
        # client creation, regardless of caller (serializer or ORM).
        if is_new and self.opening_pending_amount:
            ClientLedgerEntry.objects.create(
                client=self,
                entry_type="OPENING_BALANCE",
                amount=self.opening_pending_amount,
                note="Opening balance (entered at client creation)",
            )

    @property
    def has_pending_marker(self):
        return self.pending_as_of_date is not None

    def ledger_entries_chronological(self):
        """
        Live (non-deleted) ledger entries, oldest first. The ledger tab, the
        running balances and the marker math all walk this one list.
        """
        return list(
            self.ledger_entries.filter(is_deleted=False)
            .select_related("related_delivery__sku")
            .order_by("date", "id")
        )

    @property
    def pending_amount(self):
        """
        Live pending balance — the ledger sum is the single source of truth
        (spec 3.2). When the user has marked a pending amount as of a date, that
        figure replaces everything dated on or before that date and only ledger
        entries dated strictly after it are added on top.
        """
        from django.db.models import Sum

        qs = self.ledger_entries.filter(is_deleted=False)
        total = Decimal("0.00")
        if self.pending_as_of_date is None:
            total = qs.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        else:
            later = qs.filter(date__gt=self.pending_as_of_date).aggregate(
                s=Sum("amount")
            )["s"] or Decimal("0.00")
            total = self.pending_as_of_amount + later
        return money(total)

    def pending_breakdown(self, as_of=None, amount=None):
        """
        Pending split for the marker, in the payload shape shared by the client
        and vendor marker dialogs:
            amount (the entered figure) + after.total (later entries) = total
        `before` lists the superseded entries and `pure_total` shows what the
        balance would be with no marker at all. Pass `as_of`/`amount` to preview
        a candidate marker without saving it.
        """
        rows = [_ledger_item(e) for e in self.ledger_entries_chronological()]
        applied, superseded = _split_items(rows, as_of)
        pure = sum((r["amount"] for r in rows), Decimal("0"))
        if as_of is None:
            base = Decimal("0")
            after_total = pure
        else:
            base = self.pending_as_of_amount if amount is None else amount
            after_total = sum((r["amount"] for r in applied), Decimal("0"))
        return _marker_payload(
            as_of, base, after_total, applied, superseded, pure
        )


class ClientSKUPrice(models.Model):
    """Preferred SKU + selling price per case for a client (spec 3.1)."""

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="sku_prices"
    )
    sku = models.ForeignKey(
        "SKU", on_delete=models.CASCADE, related_name="client_prices"
    )
    selling_price_per_case = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        unique_together = ("client", "sku")
        ordering = ["client__name", "sku__description"]

    def __str__(self):
        return f"{self.client.name} / {self.sku.description} @ {self.selling_price_per_case}"


# ---------------------------------------------------------------------------
# 3.2 ClientLedgerEntry (append-only audit log with edit/delete support)
# ---------------------------------------------------------------------------
class ClientLedgerEntry(models.Model):
    ENTRY_TYPES = [
        ("DELIVERY", "Delivery"),
        ("PAYMENT", "Payment"),
        ("ADJUSTMENT", "Adjustment"),
        ("OPENING_BALANCE", "Opening Balance"),
    ]

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="ledger_entries"
    )
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPES)
    # Positive = increases pending (delivery), negative = decreases (payment).
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    related_delivery = models.ForeignKey(
        "StockDelivery",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    note = models.TextField(blank=True, default="")
    date = models.DateField(default=timezone.now)

    # Audit fields (spec 3.7)
    is_deleted = models.BooleanField(default=False)
    is_edited = models.BooleanField(default=False)
    edit_history = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.client.name} {self.entry_type} {self.amount}"

    def _audit_snapshot(self):
        return {
            "entry_type": self.entry_type,
            "amount": str(money(self.amount)),
            "note": self.note,
            "date": self.date.isoformat(),
        }

    def save(self, *args, **kwargs):
        skip_audit = kwargs.pop("skip_audit", False)
        if not self._state.adding and not skip_audit:
            try:
                previous = ClientLedgerEntry.objects.get(pk=self.pk)
            except ClientLedgerEntry.DoesNotExist:
                previous = None
            if previous is not None:
                old_snapshot = previous._audit_snapshot()
                new_snapshot = self._audit_snapshot()
                if old_snapshot != new_snapshot:
                    history = list(self.edit_history or [])
                    history.append(
                        {
                            "timestamp": timezone.now().isoformat(),
                            "before": old_snapshot,
                            "after": new_snapshot,
                        }
                    )
                    self.edit_history = history
                    self.is_edited = True
        super().save(*args, **kwargs)

    def soft_delete(self):
        history = list(self.edit_history or [])
        history.append(
            {
                "timestamp": timezone.now().isoformat(),
                "action": "deleted",
                "before": self._audit_snapshot(),
            }
        )
        self.edit_history = history
        self.is_deleted = True
        self.save(skip_audit=True)


# ---------------------------------------------------------------------------
# 3.3 Material (raw material master — "MVP Master")
# ---------------------------------------------------------------------------
class Material(models.Model):
    CATEGORIES = [
        ("Bottle", "Bottle"),
        ("Cap", "Cap"),
        ("Label", "Label"),
        ("Other", "Other"),
    ]

    name = models.CharField(max_length=200, unique=True)
    category = models.CharField(max_length=50, choices=CATEGORIES, default="Other")
    # Custom labels belong to exactly one client (spec 3.3).
    is_client_specific = models.BooleanField(default=False)
    client = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="materials",
    )
    # Selected from the Vendor master list (contact + amount owed live on
    # the Vendor). Kept nullable so generic stock can exist without a vendor.
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="materials",
    )
    # Convenience/display field only — actual costing always uses FIFO batches.
    current_price_per_unit = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    stock_alert_qty = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    wastage_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    # Everything is transacted in CASES (bottles are never considered).
    unit_of_measure = models.CharField(max_length=30, default="cases")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.is_client_specific and not self.client:
            raise ValidationError(
                {"client": "Client-specific materials must be linked to a client."}
            )
        if not self.is_client_specific:
            self.client = None

    @property
    def stock_in_hand(self):
        """Live sum of MaterialBatch.quantity_remaining (excl. soft-deleted)."""
        from django.db.models import Sum

        return (
            self.batches.filter(is_deleted=False).aggregate(s=Sum("quantity_remaining"))["s"]
            or Decimal("0.0000")
        )

    @property
    def is_below_alert(self):
        return self.stock_in_hand < self.stock_alert_qty


# ---------------------------------------------------------------------------
# 3.4 MaterialBatch (FIFO queue — one row per "arrived stock" event)
#    3.9 ArrivedStock is just the creation flow for this same table.
# ---------------------------------------------------------------------------
class MaterialBatch(AuditModel):
    material = models.ForeignKey(
        Material, on_delete=models.CASCADE, related_name="batches"
    )
    quantity_received = models.DecimalField(max_digits=12, decimal_places=4)
    # Drives FIFO: decremented as deliveries consume this batch.
    quantity_remaining = models.DecimalField(max_digits=12, decimal_places=4)
    # Landed price for THIS batch — may differ from master's display price.
    price_per_unit = models.DecimalField(max_digits=12, decimal_places=2)
    arrival_date = models.DateField(default=timezone.now)
    note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["arrival_date", "id"]  # FIFO: oldest first

    def __str__(self):
        return (
            f"{self.material.name} batch #{self.pk} "
            f"({self.quantity_remaining}/{self.quantity_received})"
        )

    @property
    def consumed_quantity(self):
        """Cases already taken out of this batch by FIFO (received − remaining)."""
        return qty(self.quantity_received - self.quantity_remaining)

    def _audit_snapshot(self):
        return {
            "material": self.material_id,
            "quantity_received": str(self.quantity_received),
            "quantity_remaining": str(self.quantity_remaining),
            "price_per_unit": str(self.price_per_unit),
            "arrival_date": self.arrival_date.isoformat(),
            "note": self.note,
        }

    def save(self, *args, **kwargs):
        """
        Audit handled by AuditModel.save -> _run_audit using _audit_snapshot.

        User request: the inward-material line items on the Home screen are
        editable. When `quantity_received` is corrected, `quantity_remaining`
        moves by the SAME delta, so:
        - cases already consumed stay consumed (the consumed quantity is the
          anchor, not the received quantity),
        - stock in hand, the FIFO queue, the vendor payable and shortfall checks
          all follow the new figure immediately,
        - shrinking a batch below what was already consumed drives the remainder
          negative — the same convention the FIFO shortfall path uses — so the
          discrepancy stays visible and can be reconciled by a Stock Adjustment.

        The FIFO engine itself only ever changes `quantity_remaining`, so its
        bookkeeping saves are a no-op here (delta = 0).
        """
        if self._state.adding:
            if self.quantity_remaining is None:
                self.quantity_remaining = self.quantity_received
        elif self.pk:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values_list("quantity_received", flat=True)
                .first()
            )
            if previous is not None and previous != self.quantity_received:
                self.quantity_remaining = qty(
                    self.quantity_remaining + (self.quantity_received - previous)
                )
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# 3.5 SKU + requirements, 3.6 SKUPrintCost
# ---------------------------------------------------------------------------
class SKU(models.Model):
    description = models.CharField(max_length=200, unique=True)
    qty_per_case = models.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal("24")
    )
    volume_ml = models.PositiveIntegerField(default=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["description"]

    def __str__(self):
        return self.description


class SKUMaterialRequirement(models.Model):
    """
    Through table: SKU x material x qty_per_CASE.
    All quantities are per case of the finished SKU — bottles are never
    considered anywhere (user requirement). Decimals allowed (e.g. 1 case
    of bottles, 1.08 sheets of labels per case).
    """

    sku = models.ForeignKey(SKU, on_delete=models.CASCADE, related_name="requirements")
    material = models.ForeignKey(
        Material, on_delete=models.CASCADE, related_name="sku_requirements"
    )
    qty_per_case = models.DecimalField(max_digits=12, decimal_places=4)

    class Meta:
        unique_together = ("sku", "material")
        ordering = ["sku__description", "material__name"]

    def __str__(self):
        return f"{self.sku.description}: {self.material.name} x {self.qty_per_case}/case"


class SKUPrintCost(models.Model):
    """
    Per-SKU print/label cost config (spec 3.6).
    Per-label cost = (paper_cost + print_cost_per_paper) / labels_per_paper,
    then wastage-adjusted.
    """

    sku = models.OneToOneField(SKU, on_delete=models.CASCADE, related_name="print_cost")
    paper_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    print_cost_per_paper = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    labels_per_paper = models.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal("1")
    )
    wastage_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    def __str__(self):
        return f"Print cost for {self.sku.description}"


# ---------------------------------------------------------------------------
# 3.8 StockDelivery
# ---------------------------------------------------------------------------
class StockDelivery(AuditModel):
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="deliveries"
    )
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, related_name="deliveries")
    date = models.DateField(default=timezone.now)
    qty_cases = models.DecimalField(max_digits=12, decimal_places=4)
    # Auto-filled from ClientSKUPrice at entry time, editable per-delivery
    # (override capability confirmed by user for v1 — spec 3.8 / assumption 3).
    selling_price_per_case = models.DecimalField(max_digits=12, decimal_places=2)
    # Creation-time DIRECT cost per case (raw materials consumed from the
    # FIFO batches + print/label), written ONCE for the AUDIT trail (3.7).
    # Displayed costs are never frozen (user request — no frozen costs
    # anywhere): they are recomputed from today's prices via
    # cogs.dynamic_costs_for, see base_cogs_per_case_current below.
    base_cogs_per_case_snapshot = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    # Full per-case COGS as it stood at creation (base + that month's overhead
    # allocation at the time). Audit only — the value the UI shows is computed
    # DYNAMICALLY (user request): current direct cost + the delivery month's
    # CURRENT overhead per case, so later price / print-config / overhead /
    # labour edits flow through to every delivery.
    cogs_per_case_snapshot = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    # True if delivery completed despite insufficient raw material stock.
    stock_shortfall_flag = models.BooleanField(default=False)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.date} {self.client.name} {self.sku.description} x{self.qty_cases}"

    @property
    def overhead_per_case_current(self):
        """
        Live overhead allocation for this delivery's month:
        month overhead total / cases sold that month (user request — the
        overhead component is dynamic, not frozen).
        """
        from . import cogs

        return cogs.overhead_per_case(cogs.month_key(self.date))

    @property
    def base_cogs_per_case_current(self):
        """
        Current DIRECT cost per case (raw materials + print), recomputed at
        read time from TODAY's FIFO queue prices and the SKU's CURRENT print
        config (user request — no frozen costs; the snapshot field above is
        the creation-time audit record only).
        """
        from . import cogs

        costs = cogs.dynamic_costs_for([self])
        direct = costs[self.id]["direct"]
        if self.qty_cases:
            return money(direct / self.qty_cases)
        return money(direct)

    @property
    def cogs_per_case_current(self):
        """Current direct cost per case + current month overhead per case."""
        return money(self.base_cogs_per_case_current + self.overhead_per_case_current)

    @property
    def total_amount(self):
        return money(self.qty_cases * self.selling_price_per_case)

    @property
    def total_cogs(self):
        return money(self.qty_cases * self.cogs_per_case_current)

    def _audit_snapshot(self):
        return {
            "client": self.client_id,
            "sku": self.sku_id,
            "date": self.date.isoformat(),
            "qty_cases": str(self.qty_cases),
            "selling_price_per_case": str(self.selling_price_per_case),
            "base_cogs_per_case_snapshot": str(self.base_cogs_per_case_snapshot),
            "cogs_per_case_snapshot": str(self.cogs_per_case_snapshot),
            "stock_shortfall_flag": self.stock_shortfall_flag,
        }


# ---------------------------------------------------------------------------
# 4.4 StockAdjustment — signed adjustment record layered on top of the FIFO
# queue (confirmed with user — assumption 2). A positive adjustment is booked as
# a new zero-price MaterialBatch (enters FIFO queue at the back, oldest-first
# order preserved); a negative adjustment consumes from the oldest batches using
# the same FIFO walker as deliveries (so negative stock is possible and visible).
# Both directions are recorded here with a mandatory reason for the audit view.
# Note (user request): a wrongly keyed arrival is now corrected on the Home
# screen by editing the MaterialBatch line item itself — that is an audited,
# deliberate edit (see MaterialBatch.save), while unaccounted differences still
# go through a StockAdjustment with a reason.
# ---------------------------------------------------------------------------
class StockAdjustment(models.Model):
    material = models.ForeignKey(
        Material, on_delete=models.CASCADE, related_name="adjustments"
    )
    # Signed: positive = add stock, negative = remove stock.
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    reason = models.TextField()
    date = models.DateField(default=timezone.now)
    # Batch created (positive) or batches consumed list (negative), for trace.
    reference_batches = models.JSONField(default=list, blank=True)
    is_deleted = models.BooleanField(default=False)
    is_edited = models.BooleanField(default=False)
    edit_history = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Adjust {self.material.name} {self.quantity:+} ({self.date})"

    def _audit_snapshot(self):
        return {
            "material": self.material_id,
            "quantity": str(self.quantity),
            "reason": self.reason,
            "date": self.date.isoformat(),
        }

    def save(self, *args, **kwargs):
        skip_audit = kwargs.pop("skip_audit", False)
        if not self._state.adding and not skip_audit:
            try:
                previous = StockAdjustment.objects.get(pk=self.pk)
            except StockAdjustment.DoesNotExist:
                previous = None
            if previous is not None:
                old_snapshot = previous._audit_snapshot()
                new_snapshot = self._audit_snapshot()
                if old_snapshot != new_snapshot:
                    history = list(self.edit_history or [])
                    history.append(
                        {
                            "timestamp": timezone.now().isoformat(),
                            "before": old_snapshot,
                            "after": new_snapshot,
                        }
                    )
                    self.edit_history = history
                    self.is_edited = True
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# 3.10 Overhead / Labour
# ---------------------------------------------------------------------------
class OverheadCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    # Pre-seeded defaults (Rent, Diesel, Electricity, Labour) are non-deletable
    # in the API; custom categories can be added/renamed/removed by the user.
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class MonthlyOverhead(models.Model):
    category = models.ForeignKey(
        OverheadCategory, on_delete=models.CASCADE, related_name="monthly_overheads"
    )
    # Year-month, e.g. 2026-09.
    month = models.CharField(max_length=7)  # YYYY-MM
    # For the Labour category this is ALWAYS auto-summed from EmployeePayment
    # rows (user request: no manual override) — see models.sync_labour_overhead.
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        unique_together = ("category", "month")
        ordering = ["-month", "category__name"]

    def __str__(self):
        return f"{self.category.name} {self.month}: {self.amount}"


class Employee(models.Model):
    name = models.CharField(max_length=200)
    role = models.CharField(max_length=100, blank=True, default="")
    monthly_pay = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class EmployeePayment(models.Model):
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="payments"
    )
    month = models.CharField(max_length=7)  # YYYY-MM
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField(default=timezone.now)
    note = models.TextField(blank=True, default="")
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.employee.name} {self.month}: {self.amount_paid}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Roll up into the Labour category's monthly overhead automatically
        # (spec 3.10 — no double entry). Labour is always the exact sum of
        # individual payments; manual overrides are not allowed.
        sync_labour_overhead(self.month)


def sync_labour_overhead(month: str):
    """
    Labour overhead for `month` is ALWAYS the sum of that month's individual
    labour payments (user request: no manual override). Called on every
    EmployeePayment save/delete; also enforced by the API so a direct edit of
    the Labour row is ignored and replaced by the rolled-up figure.
    """
    from django.db.models import Sum

    labour_cat, _ = OverheadCategory.objects.get_or_create(
        name="Labour", defaults={"is_default": True}
    )
    total = (
        EmployeePayment.objects.filter(month=month, is_deleted=False).aggregate(
            s=Sum("amount_paid")
        )["s"]
        or Decimal("0")
    )
    row, created = MonthlyOverhead.objects.get_or_create(
        category=labour_cat, month=month, defaults={"amount": total}
    )
    if not created and row.amount != total:
        row.amount = total
        row.save()
    return row





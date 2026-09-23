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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def total_purchased(self):
        """Landed value of all arrived stock from this vendor's materials."""
        from django.db.models import Sum

        total = (
            MaterialBatch.objects.filter(
                material__vendor=self, is_deleted=False
            ).aggregate(s=Sum(F("quantity_received") * F("price_per_unit")))["s"]
            or Decimal("0")
        )
        return money(total)

    @property
    def total_paid(self):
        from django.db.models import Sum

        total = (
            VendorPayment.objects.filter(
                vendor=self, is_deleted=False
            ).aggregate(s=Sum("amount"))["s"]
            or Decimal("0")
        )
        return money(total)

    @property
    def amount_owed(self):
        """What we still owe: purchases received - payments made (INR)."""
        return money(self.total_purchased - self.total_paid)


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
    def pending_amount(self):
        """Live sum of ledger entries — the single source of truth (spec 3.2)."""
        from django.db.models import Sum

        total = (
            self.ledger_entries.filter(is_deleted=False).aggregate(s=Sum("amount"))["s"]
            or Decimal("0.00")
        )
        return money(total)


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
        # Audit handled by AuditModel.save -> _run_audit using _audit_snapshot.
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
    # Frozen DIRECT cost per case at creation (raw materials consumed from the
    # FIFO batches + print/label). This is a physical fact — the batches were
    # really consumed at those prices — so it never changes.
    base_cogs_per_case_snapshot = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    # Full per-case COGS as it stood at creation (base + that month's overhead
    # allocation at the time). Kept for the audit trail only — the value the UI
    # shows is computed DYNAMICALLY (user request): base + the delivery month's
    # CURRENT overhead per case, so later overhead/labour entries for the month
    # flow through to every delivery of that month.
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
    def cogs_per_case_current(self):
        """base (frozen direct cost) + current month overhead per case."""
        return money(self.base_cogs_per_case_snapshot + self.overhead_per_case_current)

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
# queue (confirmed with user — assumption 2). We deliberately do NOT edit
# MaterialBatch rows silently. A positive adjustment is booked as a new
# zero-price MaterialBatch (enters FIFO queue at the back, oldest-first order
# preserved); a negative adjustment consumes from the oldest batches using the
# same FIFO walker as deliveries (so negative stock is possible and visible).
# Both directions are recorded here with a mandatory reason for the audit view.
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





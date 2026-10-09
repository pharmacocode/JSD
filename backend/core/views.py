"""DRF views for the JSD Group API."""

import json
import urllib.request
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from . import cogs
from . import perfcache
from .cogs import (
    DeliveryShortfall,
    apply_stock_adjustment,
    create_deliveries,
    create_delivery,
)
from .models import (
    Client,
    ClientLedgerEntry,
    ClientOrder,
    ClientSKUPrice,
    Employee,
    EmployeePayment,
    LoginAttempt,
    Material,
    MaterialBatch,
    MonthlyOverhead,
    OverheadCategory,
    SKU,
    SKUMaterialRequirement,
    SKUPrintCost,
    StockAdjustment,
    StockDelivery,
    Vendor,
    VendorPayment,
    dstr,
    money,
)
from .serializers import (
    ClientLedgerEntrySerializer,
    ClientOrderSerializer,
    ClientOrderWriteSerializer,
    ClientSerializer,
    ClientSKUPriceSerializer,
    EmployeePaymentSerializer,
    EmployeeSerializer,
    MaterialBatchSerializer,
    MaterialSerializer,
    MonthlyOverheadSerializer,
    OverheadCategorySerializer,
    SKUPrintCostSerializer,
    SKUSerializer,
    SKUMaterialRequirementSerializer,
    StockAdjustmentSerializer,
    StockDeliverySerializer,
    VendorPaymentSerializer,
    VendorSerializer,
)


def month_param(request, default=None) -> str:
    value = request.query_params.get("month")
    if value:
        return value
    if default:
        return default
    return timezone.now().strftime("%Y-%m")


def sync_labour_overhead(month: str):
    """Delegates to the model-level roll-up (core.models.sync_labour_overhead)."""
    from .models import sync_labour_overhead as _sync

    _sync(month)


def marker_request(request):
    """
    Shared parser for the pending/payable "as of" marker payloads:
    {"amount": "12000", "date": "YYYY-MM-DD"}. Returns (amount, as_of, error);
    the caller turns `error` into a 400.
    """
    try:
        amount = money(Decimal(str(request.data.get("amount"))))
    except (InvalidOperation, TypeError, ValueError):
        return None, None, "amount is required"
    raw_date = request.data.get("date") or request.data.get("as_of") or ""
    as_of = parse_date(str(raw_date))
    if as_of is None:
        return None, None, "date is required (YYYY-MM-DD)"
    return amount, as_of, None


def marker_preview_amount(request):
    """?amount= for a preview GET (0 when absent/invalid)."""
    try:
        return money(Decimal(str(request.query_params.get("amount"))))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def delivery_payment_request(request):
    """
    Optional payment sent with a delivery (user request: the payment can be
    noted while entering the delivery).

    Preferred shape: {"payment": {"amount", "date"?, "note"?}} â€” the flat
    `payment_amount` / `payment_date` / `payment_note` keys work too. An absent
    or blank amount returns (None, None): no payment, the delivery is saved on
    credit exactly as before. Amount/date validation lives in
    cogs.create_delivery_payment, which turns a bad value into ValueError ->
    400 inside the delivery's transaction.
    """
    raw = request.data.get("payment")
    if isinstance(raw, dict):
        amount = raw.get("amount")
        date_value = raw.get("date")
        note = raw.get("note", "")
    else:
        amount = request.data.get("payment_amount")
        date_value = request.data.get("payment_date")
        note = request.data.get("payment_note", "")
    if amount in (None, ""):
        return None, None
    return {"amount": amount, "date": date_value, "note": note}, None


def _pending_amount_map(clients):
    """
    Live pending balance for every client in `clients` from ONE ledger query.

    Same semantics as `Client.pending_amount` (marker date replaces everything
    dated on/before it; entries strictly after it are added on top) summed
    over the grouped rows in Python, because each marked client has its own
    cutoff date.

    Returns {client_id: "DDDD.DD"} strings â€” identical to the DecimalField
    the serializer used before.
    """
    from collections import defaultdict
    from decimal import Decimal

    ids = [c.id for c in clients]
    if not ids:
        return {}
    markers = {c.id: (c.pending_as_of_date, c.pending_as_of_amount) for c in clients}
    sums = defaultdict(lambda: Decimal("0"))
    for cid, entry_date, amount in ClientLedgerEntry.objects.filter(
        is_deleted=False, client_id__in=ids
    ).values_list("client_id", "date", "amount"):
        marker_date, _ = markers.get(cid, (None, None))
        if marker_date is None or entry_date > marker_date:
            sums[cid] += amount
    out = {}
    for c in clients:
        marker_date, marker_amount = markers[c.id]
        if marker_date is None:
            out[c.id] = str(money(sums[c.id]))
        else:
            out[c.id] = str(money(marker_amount + sums[c.id]))
    return out


class ClientViewSet(viewsets.ModelViewSet):
    queryset = Client.objects.all().prefetch_related("sku_prices__sku")
    serializer_class = ClientSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.action == "list":
            # ONE ledger query for the whole page instead of 1-2 PER ROW
            # (the list did 42 queries for 17 clients before).
            context["pending_map"] = _pending_amount_map(
                list(self.filter_queryset(self.get_queryset()))
            )
        return context

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(contact_number__icontains=search)
            )
        return qs

    @action(detail=True, methods=["get", "post"])
    def ledger(self, request, pk=None):
        """
        GET  -> chronological ledger with running balance per entry.
        POST -> record a payment/adjustment:
                {amount, date?, note?, entry_type?='PAYMENT'}
        """
        client = self.get_object()
        if request.method == "POST":
            serializer = ClientLedgerEntrySerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            entry_type = serializer.validated_data.get("entry_type", "PAYMENT")
            entry = ClientLedgerEntry.objects.create(
                client=client,
                entry_type=entry_type,
                amount=serializer.validated_data["amount"],
                note=serializer.validated_data.get("note", ""),
                date=serializer.validated_data.get("date", timezone.now().date()),
            )
            return Response(
                ClientLedgerEntrySerializer(entry).data,
                status=status.HTTP_201_CREATED,
            )

        entries = client.ledger_entries_chronological()
        as_of = client.pending_as_of_date
        # Deleted rows are shown greyed out and marked deleted (user request:
        # the audit trail stays visible), but they must not affect the running
        # balance or the pending amount â€” both are live sums that exclude them.
        deleted_rows = list(
            client.ledger_entries.filter(is_deleted=True).order_by("-date", "-id")
        )
        deleted_ids = {e.id for e in deleted_rows}
        # Running balance computed chronologically (oldest first). With a
        # pending marker the walk starts from the entered figure and entries
        # dated on/before the marked date are flagged as superseded (they are
        # already inside that figure and must not be counted twice).
        balances = {}
        superseded = set()
        running = money(client.pending_as_of_amount) if as_of else Decimal("0")
        for e in entries:
            if as_of is not None and e.date <= as_of:
                superseded.add(e.id)
                continue
            running += e.amount
            balances[e.id] = str(money(running))
        # Live rows newest-first as before; the deleted ones are appended after
        # them, oldest-first inside that block, so they read as a separate
        # "deleted" section without disturbing the live running balance.
        ordered = list(reversed(entries)) + list(
            reversed(deleted_rows)
        )
        serializer = ClientLedgerEntrySerializer(
            ordered,
            many=True,
            context={
                "balances": balances,
                "superseded": superseded,
                "deleted": deleted_ids,
            },
        )
        superseded_total = sum(
            (e.amount for e in entries if e.id in superseded), Decimal("0")
        )
        return Response(
            {
                "pending_amount": str(client.pending_amount),
                "pending_as_of_amount": str(money(client.pending_as_of_amount)),
                "pending_as_of_date": as_of.isoformat() if as_of else None,
                "marker_active": as_of is not None,
                "superseded_count": len(superseded),
                "superseded_total": str(money(superseded_total)),
                "deleted_count": len(deleted_ids),
                "entries": serializer.data,
            }
        )

    @action(detail=True, methods=["post"], url_path="payment")
    def payment(self, request, pk=None):
        """Record a payment: {amount (positive), date?, note?} -> PAYMENT entry."""
        client = self.get_object()
        try:
            amount = Decimal(str(request.data.get("amount")))
        except Exception:
            return Response(
                {"detail": "amount is required"}, status=status.HTTP_400_BAD_REQUEST
            )
        if amount <= 0:
            return Response(
                {"detail": "amount must be positive"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        entry = ClientLedgerEntry.objects.create(
            client=client,
            entry_type="PAYMENT",
            amount=-amount,  # payments decrease pending (spec 3.2)
            note=request.data.get("note", ""),
            date=request.data.get("date") or timezone.now().date(),
        )
        return Response(
            {
                "entry": ClientLedgerEntrySerializer(entry).data,
                "pending_amount": str(client.pending_amount),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get", "post", "delete"], url_path="pending")
    def pending(self, request, pk=None):
        """
        Pending-amount marker (user request): the pending figure is editable at
        any time, together with the date it refers to. Ledger entries dated
        strictly after that date are added on top of it; older ones are already
        inside it.

        GET    -> breakdown; ?as_of=YYYY-MM-DD&amount=X previews without saving.
        POST   -> {"amount": "12000", "date": "2026-09-25"} saves the marker.
        DELETE -> clears the marker (pending = plain live ledger sum again).
        """
        client = self.get_object()
        if request.method == "POST":
            amount, as_of, error = marker_request(request)
            if error:
                return Response(
                    {"detail": error}, status=status.HTTP_400_BAD_REQUEST
                )
            client.pending_as_of_amount = amount
            client.pending_as_of_date = as_of
            client.save(
                update_fields=["pending_as_of_amount", "pending_as_of_date"]
            )
            return self._pending_payload(client)
        if request.method == "DELETE":
            client.pending_as_of_amount = Decimal("0")
            client.pending_as_of_date = None
            client.save(
                update_fields=["pending_as_of_amount", "pending_as_of_date"]
            )
            return self._pending_payload(client)
        if "as_of" in request.query_params:
            return self._pending_payload(
                client,
                parse_date(request.query_params.get("as_of") or ""),
                marker_preview_amount(request),
            )
        return self._pending_payload(client)

    @staticmethod
    def _pending_payload(client, as_of=None, amount=None):
        """
        Marker breakdown + the resulting live pending. Called with no arguments
        it reflects the client's stored marker; with `as_of`/`amount` it previews
        a candidate marker.
        """
        if as_of is None and amount is None:
            as_of = client.pending_as_of_date
            amount = client.pending_as_of_amount if as_of else None
        data = client.pending_breakdown(as_of, amount)
        data["pending_amount"] = str(client.pending_amount)
        return Response(data)

    @action(detail=True, methods=["get"])
    def deliveries(self, request, pk=None):
        """Client delivery history (Deliveries tab, spec 4.5)."""
        client = self.get_object()
        qs = client.deliveries.filter(is_deleted=False)
        serializer = StockDeliverySerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def lifetime(self, request, pk=None):
        """
        Lifetime revenue & profit for this client (Client Detail, user
        request). Computed over every non-deleted delivery at READ time so
        it always reflects current prices:
          revenue  = qty_cases x selling price
          direct   = cogs.dynamic_costs_for() â€” current FIFO material + print
          overhead = this client's share: per delivery, qty_cases x that
                     month's overhead-per-case (spec 6.3), i.e. the client
                     carries overhead only for the months it bought in.
        profit_with_overhead    = revenue - direct - overhead
        profit_without_overhead = revenue - direct
        """
        client = self.get_object()
        deliveries = list(client.deliveries.filter(is_deleted=False))
        revenue = sum(
            (d.qty_cases * d.selling_price_per_case for d in deliveries),
            Decimal("0"),
        )
        direct = sum(
            (row["direct"] for row in cogs.dynamic_costs_for(deliveries).values()),
            Decimal("0"),
        )
        # Overhead rate per delivery month, memoized â€” many deliveries share
        # a month and each rate is a small aggregate of its own.
        rates = {}
        overhead = Decimal("0")
        for d in deliveries:
            month = d.date.strftime("%Y-%m")
            if month not in rates:
                rates[month] = cogs.overhead_per_case(month)
            overhead += d.qty_cases * rates[month]
        cases = sum((d.qty_cases for d in deliveries), Decimal("0"))
        return Response(
            {
                "delivery_count": len(deliveries),
                "cases": dstr(cases),
                "revenue": str(money(revenue)),
                "direct_cost": str(money(direct)),
                "overhead": str(money(overhead)),
                "profit_with_overhead": str(money(revenue - direct - overhead)),
                "profit_without_overhead": str(money(revenue - direct)),
            }
        )


class ClientSKUPriceViewSet(viewsets.ModelViewSet):
    queryset = ClientSKUPrice.objects.all()
    serializer_class = ClientSKUPriceSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client_id = self.request.query_params.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)
        return qs


class ClientLedgerEntryViewSet(viewsets.ModelViewSet):
    """Full CRUD over ledger entries with soft delete (spec 3.7)."""

    queryset = ClientLedgerEntry.objects.all()
    serializer_class = ClientLedgerEntrySerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client_id = self.request.query_params.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)
        if self.request.query_params.get("include_deleted"):
            qs = qs  # audit-trail view includes soft-deleted rows
        else:
            qs = qs.filter(is_deleted=False)
        entry_type = self.request.query_params.get("entry_type")
        if entry_type:
            qs = qs.filter(entry_type=entry_type)
        return qs

    def perform_destroy(self, instance):
        # Cascades into cogs.void_delivery for DELIVERY rows (stock rolled
        # back, delivery removed from lists/charts). The ledger row itself is
        # kept, greyed out and marked deleted.
        instance.soft_delete()

    def destroy(self, request, *args, **kwargs):
        entry = self.get_object()
        related = entry.related_delivery
        self.perform_destroy(entry)
        payload = {"entry": ClientLedgerEntrySerializer(entry).data}
        if related is not None:
            # Let the caller show exactly what was reversed.
            related.refresh_from_db()
            payload["delivery_voided"] = {
                "delivery_id": related.pk,
                "is_deleted": related.is_deleted,
            }
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        entry = self.get_object()
        entry.restore()
        return Response(ClientLedgerEntrySerializer(entry).data)


class MaterialViewSet(viewsets.ModelViewSet):
    queryset = Material.objects.all()
    serializer_class = MaterialSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(name__icontains=search)
        client_id = self.request.query_params.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)
        return qs

    @action(detail=True, methods=["get"])
    def batches(self, request, pk=None):
        """FIFO queue for this material, oldest first (spec 4.6 MVP detail)."""
        material = self.get_object()
        if request.query_params.get("include_deleted"):
            qs = material.batches.all()
        else:
            qs = material.batches.filter(is_deleted=False)
        qs = qs.order_by("arrival_date", "id")
        serializer = MaterialBatchSerializer(qs, many=True)
        return Response(
            {
                "stock_in_hand": str(material.stock_in_hand),
                "batches": serializer.data,
            }
        )

    @action(detail=True, methods=["get", "post"])
    def adjustments(self, request, pk=None):
        """Stock adjustment for this material + audit history (spec 4.4)."""
        material = self.get_object()
        if request.method == "POST":
            try:
                signed = Decimal(str(request.data.get("quantity")))
            except Exception:
                return Response(
                    {"detail": "quantity is required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            reason = request.data.get("reason", "")
            date_value = request.data.get("date") or None
            try:
                adjustment, unrecorded = apply_stock_adjustment(
                    material, signed, reason, adjustment_date=date_value
                )
            except ValueError as exc:
                return Response(
                    {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
                )
            return Response(
                {
                    "adjustment": StockAdjustmentSerializer(adjustment).data,
                    "stock_in_hand": str(material.stock_in_hand),
                    "unrecorded_shortfall": str(unrecorded),
                },
                status=status.HTTP_201_CREATED,
            )
        qs = material.adjustments.filter(is_deleted=False)
        return Response(StockAdjustmentSerializer(qs, many=True).data)


class MaterialBatchViewSet(viewsets.ModelViewSet):
    """
    Arrived Stock entries (spec 4.3 = creation UI for MaterialBatch).
    Soft delete + edit audit per spec 3.7.
    """

    queryset = MaterialBatch.objects.all()
    serializer_class = MaterialBatchSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        material_id = self.request.query_params.get("material")
        if material_id:
            qs = qs.filter(material_id=material_id)
        if not self.request.query_params.get("include_deleted"):
            qs = qs.filter(is_deleted=False)
        return qs.order_by("arrival_date", "id")

    def perform_destroy(self, instance):
        instance.soft_delete()


class VendorViewSet(viewsets.ModelViewSet):
    """
    Vendor master: list with contact number + amount owed (purchases from
    this vendor's arrived stock minus payments made). Used by the material
    form's vendor picker.
    """

    queryset = Vendor.objects.all()
    serializer_class = VendorSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(contact_number__icontains=search)
            )
        return qs

    @action(detail=True, methods=["get", "post"])
    def payments(self, request, pk=None):
        """
        GET  -> payment history + live totals for this vendor.
        POST -> record a payment: {amount, date?, note?}
        """
        vendor = self.get_object()
        if request.method == "POST":
            try:
                amount = Decimal(str(request.data.get("amount")))
            except Exception:
                return Response(
                    {"detail": "amount is required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if amount <= 0:
                return Response(
                    {"detail": "amount must be positive"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            payment = VendorPayment.objects.create(
                vendor=vendor,
                amount=amount,
                date=request.data.get("date") or timezone.now().date(),
                note=request.data.get("note", ""),
            )
            return Response(
                {
                    "payment": VendorPaymentSerializer(payment).data,
                    **VendorSerializer(vendor).data,
                },
                status=status.HTTP_201_CREATED,
            )

        qs = VendorPayment.objects.filter(vendor=vendor, is_deleted=False)
        return Response(
            {
                "vendor": VendorSerializer(vendor).data,
                "payments": VendorPaymentSerializer(qs, many=True).data,
            }
        )

    @action(detail=True, methods=["get", "post", "delete"], url_path="payable")
    def payable(self, request, pk=None):
        """
        Payable marker (user request): what we owe this vendor is editable at any
        time, together with the date it refers to. Arrived stock and payments
        dated strictly after that date are added on top of the entered figure.

        GET    -> breakdown; ?as_of=YYYY-MM-DD&amount=X previews without saving.
        POST   -> {"amount": "12000", "date": "2026-09-25"} saves the marker.
        DELETE -> clears the marker (payable = purchases âˆ’ payments again).
        """
        vendor = self.get_object()
        if request.method == "POST":
            amount, as_of, error = marker_request(request)
            if error:
                return Response(
                    {"detail": error}, status=status.HTTP_400_BAD_REQUEST
                )
            vendor.payable_as_of_amount = amount
            vendor.payable_as_of_date = as_of
            vendor.save(
                update_fields=["payable_as_of_amount", "payable_as_of_date"]
            )
            return self._payable_payload(vendor)
        if request.method == "DELETE":
            vendor.payable_as_of_amount = Decimal("0")
            vendor.payable_as_of_date = None
            vendor.save(
                update_fields=["payable_as_of_amount", "payable_as_of_date"]
            )
            return self._payable_payload(vendor)
        if "as_of" in request.query_params:
            return self._payable_payload(
                vendor,
                parse_date(request.query_params.get("as_of") or ""),
                marker_preview_amount(request),
            )
        return self._payable_payload(vendor)

    @staticmethod
    def _payable_payload(vendor, as_of=None, amount=None):
        """
        Marker breakdown + the resulting amount owed. Called with no arguments it
        reflects the vendor's stored marker; with `as_of`/`amount` it previews a
        candidate marker.
        """
        if as_of is None and amount is None:
            as_of = vendor.payable_as_of_date
            amount = vendor.payable_as_of_amount if as_of else None
        data = vendor.payable_breakdown(as_of, amount)
        data["amount_owed"] = str(vendor.amount_owed)
        return Response(data)


class VendorPaymentViewSet(viewsets.ModelViewSet):
    queryset = VendorPayment.objects.filter(is_deleted=False)
    serializer_class = VendorPaymentSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        vendor_id = self.request.query_params.get("vendor")
        if vendor_id:
            qs = qs.filter(vendor_id=vendor_id)
        return qs

    def perform_destroy(self, instance):
        instance.is_deleted = True
        instance.save()


class SKUViewSet(viewsets.ModelViewSet):
    queryset = SKU.objects.all().prefetch_related(
        "requirements__material", "print_cost"
    )
    serializer_class = SKUSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(description__icontains=search)
        return qs

    @action(detail=True, methods=["get"])
    def cost_breakup(self, request, pk=None):
        """
        Cost breakup (spec 4.6 SKU detail) â€” PER CASE ONLY (bottles are never
        considered anywhere). Uses current FIFO batch prices + the live
        overhead allocation â€” like every other cost figure in the app it is
        recomputed from current data (no frozen values; snapshot fields on
        historical deliveries are audit-only).
        """
        sku = self.get_object()
        client_id = request.query_params.get("client")
        client = None
        if client_id:
            client = Client.objects.filter(pk=client_id).first()
        if client is None:
            client = Client.objects.order_by("id").first()
        if client is None:
            return Response(
                {"detail": "Create a client first â€” label stock resolves per client."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = cogs.estimate_cogs(
            sku, client, Decimal("1"), timezone.now().date(), preview=True
        )
        return Response(
            {
                "basis": "case",
                "per_case": result["per_case"],
                "base_per_case": result["per_case_base"],
                "display_total": result["per_case"],
                "details": result["details"],
                "note": (
                    "Estimate from current FIFO batch prices + the delivery "
                    "month's live overhead allocation, per case."
                ),
            }
        )

    @action(detail=True, methods=["get", "put", "post"], url_path="print-cost")
    def print_cost(self, request, pk=None):
        """Get or set the SKUPrintCost config (spec 3.6)."""
        sku = self.get_object()
        if request.method == "GET":
            try:
                pc = sku.print_cost
                return Response(SKUPrintCostSerializer(pc).data)
            except SKUPrintCost.DoesNotExist:
                return Response(None)
        instance, _ = SKUPrintCost.objects.get_or_create(sku=sku)
        serializer = SKUPrintCostSerializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class SKUMaterialRequirementViewSet(viewsets.ModelViewSet):
    queryset = SKUMaterialRequirement.objects.all()
    serializer_class = SKUMaterialRequirementSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        sku_id = self.request.query_params.get("sku")
        if sku_id:
            qs = qs.filter(sku_id=sku_id)
        return qs


class StockDeliveryViewSet(viewsets.ModelViewSet):
    """
    Deliveries. Custom create implements the Add-Delivery flow (spec 4.2):
    first attempt returns 409 with shortfall details when stock is short;
    resending with force=true proceeds and sets stock_shortfall_flag.
    cogs_per_case_snapshot is set server-side only (creation-time audit
    record, spec 5 â€” displayed costs stay dynamic).
    """

    queryset = StockDelivery.objects.select_related(
        "client", "sku", "sku__print_cost"
    )
    serializer_class = StockDeliverySerializer

    def get_queryset(self):
        # Half-open range (indexed) instead of EXTRACT-based year/month.
        qs = super().get_queryset()
        client_id = self.request.query_params.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)
        sku_id = self.request.query_params.get("sku")
        if sku_id:
            qs = qs.filter(sku_id=sku_id)
        month = self.request.query_params.get("month")
        if month:
            year, mon = (int(p) for p in month.split("-"))
            qs = qs.filter(**cogs.month_range(year, mon))
        if not self.request.query_params.get("include_deleted"):
            qs = qs.filter(is_deleted=False)
        return qs

    def perform_destroy(self, instance):
        """
        Deleting a delivery rolls EVERYTHING back (user request): the FIFO
        stock it consumed is returned to the batches it came from, and the
        ledger entries it created are soft-deleted so the client's pending
        amount follows. DRF's default here is a HARD delete, which would
        destroy the audit trail and silently keep the stock consumed â€” this
        is the only correct behaviour for this app.
        """
        return cogs.void_delivery(instance, reason="Delivery deleted")

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        # perform_destroy is returned rather than stashed on self: DRF builds
        # the action from the unbound method, so an attribute set inside it is
        # not reliably readable here. Returning the payload keeps it explicit.
        payload = self.perform_destroy(instance)
        if request.query_params.get("include_deleted"):
            return Response(StockDeliverySerializer(instance).data)
        # Report what was rolled back so the UI can say which materials came
        # back, instead of the stock change being invisible.
        return Response(payload, status=status.HTTP_200_OK)

    def create(self, request, *args, **kwargs):
        from datetime import date as date_cls

        client = Client.objects.filter(pk=request.data.get("client")).first()
        sku = SKU.objects.filter(pk=request.data.get("sku")).first()
        if client is None or sku is None:
            return Response(
                {"detail": "client and sku are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            qty_cases = Decimal(str(request.data.get("qty_cases")))
        except Exception:
            return Response(
                {"detail": "qty_cases is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        selling_price = request.data.get("selling_price_per_case")
        force = str(request.data.get("force", "")).lower() in ("1", "true", "yes")
        date_value = request.data.get("date")
        if date_value:
            date_value = date_cls.fromisoformat(date_value)

        # Optional payment noted while entering the delivery (user request):
        # blank -> no payment entry at all, delivery saved on credit as before.
        payment, error = delivery_payment_request(request)
        if error:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)

        try:
            delivery, result = create_delivery(
                client,
                sku,
                qty_cases,
                selling_price_per_case=(
                    Decimal(str(selling_price)) if selling_price is not None else None
                ),
                delivery_date=date_value,
                note=request.data.get("note", ""),
                force=force,
                payment=payment,
            )
        except DeliveryShortfall as exc:
            # Spec 4.2 step 4: clear warning payload -> Proceed/Cancel UI.
            return Response(
                {
                    "detail": (
                        "Insufficient stock â€” proceeding records the delivery "
                        "and stock in hand will show negative. Reconcile later "
                        "with a Stock Adjustment or a backdated stock arrival, "
                        "or cancel."
                    ),
                    "shortfall": exc.shortages,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(
            {
                "delivery": StockDeliverySerializer(delivery).data,
                "cogs": {
                    # Live figures: the actual cost just consumed + the
                    # delivery month's CURRENT overhead allocation.
                    "per_case": result["per_case_current"],
                    "per_case_at_creation": result["per_case"],
                    "base_per_case": result["base_per_case"],
                    "overhead_per_case_current": result["overhead_per_case_current"],
                    "details": result["details"],
                },
                "total_amount": result["total_amount"],
                "total_cogs_current": result["total_cogs_current"],
                "client_pending_amount": result["client_pending_amount"],
                "payment": result["payment"],
                "stock_shortfall_flag": delivery.stock_shortfall_flag,
                # No material requirements on this SKU => this delivery
                # consumed no stock (it can never go negative) â€” surfaced so
                # the screen can warn rather than stay silent.
                "no_material_requirements": result["no_material_requirements"],
                "skus_without_requirements": (
                    [sku.description] if result["no_material_requirements"] else []
                ),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        """
        Multi-SKU delivery (spec 4.2) â€” one client, one date, one note:
        POST {client, date, note, force,
              lines: [{sku, qty_cases, selling_price_per_case}],
              payment?: {amount, date?, note?}}

        Creates one StockDelivery per line (each with its own creation-time
        COGS snapshot â€” audit only â€” and generated client-ledger entry)
        inside a single transaction, so the delivery is never half-saved. The
        stock check is aggregated across the lines: a 409 carries the summed
        shortfall list
        and the same request with force=true proceeds ("Proceed anyway").

        `payment` is optional (user request): when an amount is given, the same
        atomic request writes ONE PAYMENT ledger entry for the client (negative
        amount, dated the delivery date unless stated otherwise), so the money
        is captured as a client transaction and the pending balance drops
        immediately. Omitted/blank => no payment, plain credit delivery.
        """
        from datetime import date as date_cls

        client = Client.objects.filter(pk=request.data.get("client")).first()
        if client is None:
            return Response(
                {"detail": "client is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_lines = request.data.get("lines") or []
        if not raw_lines:
            return Response(
                {"detail": "lines is required (at least one SKU with a quantity)"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        lines = []
        for raw in raw_lines:
            sku = SKU.objects.filter(pk=raw.get("sku")).first()
            if sku is None:
                return Response(
                    {"detail": "Every line needs a valid sku"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                qty_cases = Decimal(str(raw.get("qty_cases")))
            except (InvalidOperation, TypeError, ValueError):
                return Response(
                    {"detail": f"qty_cases is required for {sku.description}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            raw_price = raw.get("selling_price_per_case")
            try:
                price = (
                    Decimal(str(raw_price)) if raw_price not in (None, "") else None
                )
            except (InvalidOperation, TypeError, ValueError):
                return Response(
                    {"detail": f"Invalid selling price for {sku.description}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            lines.append({"sku": sku, "qty_cases": qty_cases,
                          "selling_price_per_case": price})

        date_value = request.data.get("date")
        date_value = date_cls.fromisoformat(date_value) if date_value else None
        force = str(request.data.get("force", "")).lower() in ("1", "true", "yes")

        # Optional payment handed over with this delivery (user request).
        payment, error = delivery_payment_request(request)
        if error:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)

        try:
            deliveries, result = create_deliveries(
                client,
                lines,
                delivery_date=date_value,
                note=request.data.get("note", ""),
                force=force,
                payment=payment,
            )
        except DeliveryShortfall as exc:
            # Spec 4.2 step 4: clear warning payload -> Proceed/Cancel UI.
            return Response(
                {
                    "detail": (
                        "Insufficient stock â€” proceeding records the delivery "
                        "and stock in hand will show negative. Reconcile later "
                        "with a Stock Adjustment or a backdated stock arrival, "
                        "or cancel."
                    ),
                    "shortfall": exc.shortages,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(
            {
                "deliveries": [StockDeliverySerializer(d).data for d in deliveries],
                "lines": result["lines"],
                "cogs": result["cogs"],
                "total_cases": result["total_cases"],
                "total_amount": result["total_amount"],
                "total_cogs_current": result["total_cogs_current"],
                "client_pending_amount": result["client_pending_amount"],
                "payment": result["payment"],
                "stock_shortfall_flag": result["stock_shortfall_flag"],
                # SKUs in this run that have no material requirements â€” they
                # consumed no stock at all (see create_deliveries).
                "no_material_requirements": result["no_material_requirements"],
                "skus_without_requirements": result["skus_without_requirements"],
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"])
    def preview(self, request):
        """Non-mutating COGS + shortfall preview for the Add-Delivery screen."""
        from datetime import date as date_cls

        client = Client.objects.filter(pk=request.data.get("client")).first()
        if client is None:
            return Response(
                {"detail": "client is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        date_value = request.data.get("date")
        date_value = (
            date_cls.fromisoformat(date_value) if date_value else timezone.now().date()
        )

        # Multi-SKU preview â€” POST {client, date, lines: [{sku, qty_cases}]}:
        # one card for the whole delivery (per-line breakdown + aggregate
        # totals + the shortfall summed per material across the lines).
        raw_lines = request.data.get("lines")
        if raw_lines is not None:
            parsed = []
            for raw in raw_lines:
                line_sku = SKU.objects.filter(pk=raw.get("sku")).first()
                if line_sku is None:
                    return Response(
                        {"detail": "Every line needs a valid sku"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                try:
                    line_qty = Decimal(str(raw.get("qty_cases")))
                except (InvalidOperation, TypeError, ValueError):
                    return Response(
                        {
                            "detail": (
                                f"qty_cases is required for {line_sku.description}"
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                parsed.append({"sku": line_sku, "qty_cases": line_qty})
            return Response(cogs.preview_deliveries(client, parsed, date_value))

        sku = SKU.objects.filter(pk=request.data.get("sku")).first()
        if sku is None:
            return Response(
                {"detail": "sku is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qty_cases = Decimal(str(request.data.get("qty_cases", "1")))
        result = cogs.estimate_cogs(sku, client, qty_cases, date_value, preview=True)
        # Default selling price for display (auto-fill, spec 4.2 step 3).
        csp = ClientSKUPrice.objects.filter(client=client, sku=sku).first()
        return Response(
            {
                "cogs": {
                    "per_case": result["per_case"],
                    "base_per_case": result["per_case_base"],
                    "overhead_per_case": result["overhead_per_case"],
                    "details": result["details"],
                },
                "shortfall": result["shortfall"],
                "no_material_requirements": result["no_material_requirements"],
                "default_selling_price": (
                    str(csp.selling_price_per_case) if csp else None
                ),
            }
        )

    # NOTE: the rolling-back perform_destroy/destroy pair lives at the top of
    # this class. A second perform_destroy used to sit here and shadow it, which
    # soft-deleted the delivery while leaving its FIFO consumption booked â€”
    # exactly the "deleted but still everywhere" bug.


class StockAdjustmentViewSet(viewsets.ModelViewSet):
    """
    Stock adjustments as standalone records (spec 4.4). Creating one here
    also applies it via the engine so FIFO stays consistent.
    """

    queryset = StockAdjustment.objects.filter(is_deleted=False)
    serializer_class = StockAdjustmentSerializer

    def create(self, request, *args, **kwargs):
        material = Material.objects.filter(pk=request.data.get("material")).first()
        if material is None:
            return Response(
                {"detail": "material is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            signed = Decimal(str(request.data.get("quantity")))
            reason = request.data.get("reason", "")
            date_value = request.data.get("date") or None
            adjustment, unrecorded = apply_stock_adjustment(
                material, signed, reason, adjustment_date=date_value
            )
        except (ValueError, TypeError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                "adjustment": StockAdjustmentSerializer(adjustment).data,
                "stock_in_hand": str(material.stock_in_hand),
                "unrecorded_shortfall": str(unrecorded),
            },
            status=status.HTTP_201_CREATED,
        )

    def perform_destroy(self, instance):
        instance.is_deleted = True
        instance.save(skip_audit=True)


class OverheadCategoryViewSet(viewsets.ModelViewSet):
    queryset = OverheadCategory.objects.all()
    serializer_class = OverheadCategorySerializer

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_default:
            # Pre-seeded defaults (Rent/Diesel/Electricity/Labour) are
            # non-deletable (spec 3.10); custom categories are removable.
            return Response(
                {"detail": "Default categories cannot be deleted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)


class MonthlyOverheadViewSet(viewsets.ModelViewSet):
    queryset = MonthlyOverhead.objects.all()
    serializer_class = MonthlyOverheadSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        month = self.request.query_params.get("month")
        if month:
            qs = qs.filter(month=month)
        return qs

    def _enforce_labour_rollup(self, instance):
        """
        Labour is never entered manually â€” it is always the sum of that
        month's labour payments (user request). Any attempt to set the Labour
        row directly is ignored and the rolled-up figure is returned.
        """
        if instance.category.name == "Labour":
            instance = sync_labour_overhead(instance.month)
        return instance

    def perform_update(self, serializer):
        instance = serializer.save()
        self._enforce_labour_rollup(instance)

    def perform_create(self, serializer):
        instance = serializer.save()
        self._enforce_labour_rollup(instance)


class EmployeeViewSet(viewsets.ModelViewSet):
    queryset = Employee.objects.all()
    serializer_class = EmployeeSerializer


class EmployeePaymentViewSet(viewsets.ModelViewSet):
    queryset = EmployeePayment.objects.filter(is_deleted=False)
    serializer_class = EmployeePaymentSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        month = self.request.query_params.get("month")
        if month:
            qs = qs.filter(month=month)
        employee_id = self.request.query_params.get("employee")
        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        return qs

    def perform_create(self, serializer):
        instance = serializer.save()
        sync_labour_overhead(instance.month)

    def perform_update(self, serializer):
        instance = serializer.save()
        sync_labour_overhead(instance.month)

    def perform_destroy(self, instance):
        instance.is_deleted = True
        instance.save()
        sync_labour_overhead(instance.month)


class PingView(APIView):
    """
    Keep-alive target (user request): pinged every few minutes so a free-tier
    host never sleeps. Touches NO database on purpose â€” its whole job is to
    prove the process is awake and answering, instantly, even while the
    database is still warming up.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return Response({"ok": True, "time": timezone.now().isoformat()})


def _orders_payload():
    """
    Pending + Ready-to-Deliver orders and the per-material commitment behind
    them, in one dict. Shared by the dashboard (so Home needs a single request
    per period) and by /api/orders/summary/ (so StatusUpdate and Add Delivery
    can fetch it directly).

    `ready` deliberately includes DELIVERED orders that still have an
    undelivered line â€” a partially delivered multi-line order stays in the
    Ready bucket for the lines that are still to go out.
    """
    orders = (
        ClientOrder.objects.filter(
            is_deleted=False,
            status__in=[ClientOrder.PENDING, ClientOrder.COMPLETED, ClientOrder.DELIVERED],
        )
        .select_related("client")
        .prefetch_related("items__sku", "items__delivery")
    )
    pending = [
        ClientOrderSerializer(o).data
        for o in orders
        if o.status == ClientOrder.PENDING
    ]
    ready = [
        ClientOrderSerializer(o).data
        for o in orders
        if o.status in (ClientOrder.COMPLETED, ClientOrder.DELIVERED)
        and o.undelivered_items
    ]

    committed = cogs.committed_map()
    available = cogs.available_map()
    commitment = []
    for m in Material.objects.filter(id__in=list(committed.keys()) or [0]):
        amount = committed.get(m.id, cogs.ZERO)
        if amount <= 0:
            continue
        commitment.append(
            {
                "material_id": m.id,
                "material": m.name,
                "unit": m.unit_of_measure,
                "committed": dstr(amount),
                "available": dstr(available.get(m.id, cogs.ZERO)),
                "short": available.get(m.id, cogs.ZERO) < 0,
            }
        )
    commitment.sort(key=lambda r: r["material"])

    return {
        "pending": pending,
        "ready": ready,
        "pending_count": len(pending),
        "ready_count": len(ready),
        "commitment": commitment,
    }


class DashboardView(APIView):
    """
    Home screen data (spec 4.1, figures reworked per user request):
    - stock in hand per material (counts + alert flags for the collapsed panel)
    - monthly summary for ?month=YYYY-MM: cases sold per SKU, per-client
      revenue / profit breakdown (drives the metric chart), inward material
      arrivals with their editable batch line items, overhead summary and the
      month's profit (with and without overhead), plus a client x SKU matrix
      and the month's overhead categories for the chart drill-down breakups.
    """

    @staticmethod
    def _period(request):
        """
        Resolve the reporting period from the query string.

        - `from` AND `to` both present, parseable and from <= to -> RANGE mode,
          and the month is ignored entirely (user request: "if from and to
          dates are given, then this month selection is not applicable").
        - otherwise -> MONTH mode via ?month=YYYY-MM (the existing behaviour,
          so every current caller keeps working unchanged).

        The month is always returned: it is what the inward reconciliation and
        the overhead bucket need in month mode, and in range mode it is derived
        from `to` purely for labelling.
        """
        raw_from = request.query_params.get("from")
        raw_to = request.query_params.get("to")
        if raw_from and raw_to:
            start, end = parse_date(raw_from), parse_date(raw_to)
            if start and end and start <= end:
                return {
                    "mode": "range",
                    "month": cogs.month_key(end),
                    "from": start,
                    "to": end,
                }
        month = month_param(request)
        return {"mode": "month", "month": month, "from": None, "to": None}

    def get(self, request):
        period = self._period(request)
        month = period["month"]
        m_year, m_mon = (int(p) for p in month.split("-"))
        # A half-open date RANGE (indexed) instead of date__year/date__month
        # (EXTRACT), which a plain btree index cannot serve. Both modes share
        # the same shape, so every query below is identical either way.
        period_filter = (
            cogs.date_range(period["from"], period["to"])
            if period["mode"] == "range"
            else cogs.month_range(m_year, m_mon)
        )
        arrival_filter = {
            "arrival_date__gte": period_filter["date__gte"],
            "arrival_date__lt": period_filter["date__lt"],
        }
        # Every YYYY-MM the period touches â€” one month in month mode, all of
        # them in range mode. Overhead is keyed by month, so this is how a
        # range finds its overhead buckets and its consumption.
        months = (
            cogs.months_between(period["from"], period["to"])
            if period["mode"] == "range"
            else [month]
        )

        # Stock in hand panel. `stock_in_hand` is the LIVE batch-ledger sum;
        # `unrecorded_shortfall` is the demand-vs-booked gap (pre-negative-stock
        # legacy signature: demand recorded on deliveries that the ledger never
        # booked, so stock sits at 0 instead of the true negative). Same math
        # as the backfill command (cogs.legacy_*), read-only here.
        demand_by_material, _last_event = cogs.legacy_demand_by_material()
        gaps = cogs.legacy_shortfall_gaps(demand_by_material)
        # Stock committed to Ready-to-Deliver orders. `stock_in_hand` stays the
        # PHYSICAL batch balance (what FIFO consumes); `available` is what is
        # genuinely free, and is what the Home tiles show â€” an order that has
        # been moved to Ready to Deliver has already taken its material out of
        # Stock in Hand (user request).
        committed = cogs.committed_map()
        materials = []
        for m in Material.objects.all().order_by("name"):
            row = gaps.get(m.id, {})
            gap = row.get("gap", cogs.ZERO)
            committed_amt = committed.get(m.id, cogs.ZERO)
            materials.append(
                {
                    "id": m.id,
                    "name": m.name,
                    "category": m.category,
                    "unit": m.unit_of_measure,
                    "stock_in_hand": dstr(m.stock_in_hand),
                    "stock_alert_qty": dstr(m.stock_alert_qty),
                    # The alert state is judged on what is FREE to promise, not
                    # on the raw balance â€” stock already committed to a Ready to
                    # Deliver order is no longer sitting on the shelf.
                    "below_alert": (m.stock_in_hand - committed_amt)
                    < m.stock_alert_qty,
                    "committed": dstr(committed_amt),
                    "available": dstr(m.stock_in_hand - committed_amt),
                    "demand": dstr(row.get("demand", cogs.ZERO)),
                    "booked": dstr(row.get("booked", cogs.ZERO)),
                    "unrecorded_shortfall": dstr(gap),
                    "has_unrecorded_shortfall": gap > cogs.ZERO,
                    "is_client_specific": m.is_client_specific,
                    "client": m.client_id,
                    "client_name": m.client.name if m.client else None,
                }
            )

        # Cases sold per SKU this month â€” one grouped query for cases AND
        # revenue (the old code re-queried the month's deliveries once per SKU).
        sold = []
        for row in (
            StockDelivery.objects.filter(is_deleted=False, **period_filter)
            .values("sku_id", "sku__description")
            .annotate(
                cases=Sum("qty_cases"),
                revenue=Sum(F("qty_cases") * F("selling_price_per_case")),
            )
            .order_by("sku__description")
        ):
            sold.append(
                {
                    "sku_id": row["sku_id"],
                    "sku": row["sku__description"],
                    "cases": dstr(row["cases"] or 0),
                    "revenue": str(money(row["revenue"] or 0)),
                }
            )

        # Inward material arrivals this month: one row per material for the Home
        # summary, each carrying its underlying batch line items. Those line
        # items are editable from the Home screen (user request) â€” correcting
        # one recalculates stock in hand, FIFO and the vendor payable.
        #
        # Two extra maps back the "month reconciliation" below: how much of
        # each material this month's LIVE deliveries actually drew out of the
        # FIFO queue, and each material's current live balance. Both are
        # computed once here and reused by every row.
        # Consumption for the inward reconciliation: one month in month mode,
        # the sum of every month the range touches in range mode.
        month_consumption = {}
        for key in months:
            y, mo = (int(p) for p in key.split("-"))
            partial, _ = cogs.legacy_demand_by_material(year=y, month=mo)
            for mat_id, value in partial.items():
                month_consumption[mat_id] = (
                    month_consumption.get(mat_id, Decimal("0")) + value
                )
        material_stock = {
            m.id: m.stock_in_hand
            for m in Material.objects.filter(
                id__in=MaterialBatch.objects.filter(
                    is_deleted=False,
                    **arrival_filter,
                ).values_list("material_id", flat=True)
            )
        }
        arrivals = {}
        for batch in (
            MaterialBatch.objects.filter(
                is_deleted=False,
                **arrival_filter,
            )
            .select_related("material")
            .order_by("material__name", "arrival_date", "id")
        ):
            if batch.quantity_received <= 0 and batch.quantity_remaining < 0:
                # Deficit carrier batch (the negative-stock booking) â€” not an
                # arrival, so it must not show up as an inward line item.
                continue
            row = arrivals.setdefault(
                batch.material_id,
                {
                    "material_id": batch.material_id,
                    "material": batch.material.name,
                    "unit": batch.material.unit_of_measure,
                    "received": Decimal("0"),
                    "items": [],
                },
            )
            row["received"] += batch.quantity_received
            row["items"].append(
                {
                    "id": batch.id,
                    "date": batch.arrival_date.isoformat(),
                    "quantity_received": dstr(batch.quantity_received),
                    "quantity_remaining": dstr(batch.quantity_remaining),
                    "consumed": dstr(batch.consumed_quantity),
                    "price_per_unit": str(money(batch.price_per_unit)),
                    "value": str(
                        money(batch.quantity_received * batch.price_per_unit)
                    ),
                    "note": batch.note,
                    "is_edited": batch.is_edited,
                }
            )
        inward = []
        for row in arrivals.values():
            row["quantity"] = dstr(row.pop("received"))
            # Month reconciliation (user report: "we had 1000 ml stock in
            # September but not in October â€” mismatch"). The inward list is
            # scoped to ARRIVAL DATE, while the stock panel is a live balance,
            # so a material whose cases arrived in an earlier month shows zero
            # inward in October even though it is very much in stock. Each row
            # now also carries how much of it was CONSUMED this month and the
            # live in-hand figure, so the three numbers reconcile:
            #   opening + inward - consumed = in hand.
            mat_id = row["material_id"]
            row["consumed"] = dstr(month_consumption.get(mat_id, Decimal("0")))
            row["stock_in_hand"] = dstr(
                material_stock.get(mat_id, Decimal("0"))
            )
            inward.append(row)

        # Per-client breakdown this month: revenue, the CURRENT direct cost
        # and profit with/without the month's overhead. Powers the Home metric
        # chart (revenue / profit per client, highest first). Costs are
        # DYNAMIC (user request â€” no frozen snapshots): raw materials are
        # recomputed from today's FIFO queue and print from the SKU's current
        # print config in ONE pass (cogs.dynamic_costs_for), so a price /
        # print-config edit moves every chart immediately. The same pass also
        # accumulates a client x SKU matrix (chart drill-down breakups) so
        # every figure below derives from one identical walk over the
        # deliveries â€” bars and drill rows always add up exactly.
        # Overhead for the period. In month mode this is the month's bucket
        # and its per-case rate, exactly as before. In range mode it is the
        # sum of the buckets of every month the range touches. A DELIVERY is
        # always allocated its OWN month's per-case rate (overhead_rate_for
        # below) rather than the range's headline rate, so a multi-month
        # breakdown still adds up to the paise â€” and for a single month the
        # per-month rate IS the headline rate, so nothing changes there.
        overhead_total = (
            cogs.overhead_for_range(period["from"], period["to"])
            if period["mode"] == "range"
            else cogs.overhead_for_month(month)
        )
        overhead_per_case_month = (
            cogs.overhead_per_case_range(period["from"], period["to"])
            if period["mode"] == "range"
            else cogs.overhead_per_case(month)
        )
        _rate_cache = {}

        def overhead_rate_for(delivery):
            key = cogs.month_key(delivery.date)
            if key not in _rate_cache:
                _rate_cache[key] = cogs.overhead_per_case(key)
            return _rate_cache[key]

        month_deliveries = list(
            StockDelivery.objects.filter(
                is_deleted=False, **period_filter
            ).select_related("client", "sku", "sku__print_cost")
        )
        dynamic = cogs.dynamic_costs_for(month_deliveries)
        per_client = {}
        per_client_sku = {}
        total_revenue = Decimal("0")
        total_direct_cost = Decimal("0")
        total_cases = Decimal("0")
        for d in month_deliveries:
            revenue = d.qty_cases * d.selling_price_per_case
            costs = dynamic[d.id]
            direct_cost = costs["direct"]
            total_revenue += revenue
            total_direct_cost += direct_cost
            total_cases += d.qty_cases
            row = per_client.setdefault(
                d.client_id,
                {
                    "client": d.client.name,
                    "cases": Decimal("0"),
                    "revenue": Decimal("0"),
                    "direct_cost": Decimal("0"),
                },
            )
            row["cases"] += d.qty_cases
            row["revenue"] += revenue
            row["direct_cost"] += direct_cost
            # Allocated with THIS delivery's own month rate (identical to the
            # period rate in month mode, per-delivery in range mode). Kept
            # unrounded here and rounded once per row below, so a single month
            # still matches the old `cases x rate` figure exactly.
            rate = overhead_rate_for(d)
            row["overhead"] = row.get("overhead", Decimal("0")) + (
                d.qty_cases * rate
            )
            pair = per_client_sku.setdefault(
                (d.client_id, d.sku_id),
                {
                    "client": d.client.name,
                    "sku": d.sku.description,
                    "cases": Decimal("0"),
                    "revenue": Decimal("0"),
                    "direct_cost": Decimal("0"),
                    "materials_cost": Decimal("0"),
                    "print_cost": Decimal("0"),
                    "overhead": Decimal("0"),
                },
            )
            pair["cases"] += d.qty_cases
            pair["revenue"] += revenue
            pair["direct_cost"] += direct_cost
            pair["materials_cost"] += costs["materials"]
            pair["print_cost"] += costs["print"]
            pair["overhead"] += d.qty_cases * rate

        client_breakdown = []
        for client_id, row in per_client.items():
            # Overhead is a monthly bucket allocated per case sold (spec 6.3),
            # so each client carries its own share of the period's overhead.
            overhead = money(row["overhead"])
            client_breakdown.append(
                {
                    "client_id": client_id,
                    "client": row["client"],
                    "cases": dstr(row["cases"]),
                    "revenue": str(money(row["revenue"])),
                    "direct_cost": str(money(row["direct_cost"])),
                    "overhead": str(overhead),
                    "profit_excl_overhead": str(
                        money(row["revenue"] - row["direct_cost"])
                    ),
                    "profit_incl_overhead": str(
                        money(row["revenue"] - row["direct_cost"] - overhead)
                    ),
                }
            )
        client_breakdown.sort(key=lambda c: Decimal(c["revenue"]), reverse=True)

        # Client x SKU drill-down matrix (chart breakups, user request):
        # - cases chart tap -> clients served for one SKU,
        # - revenue chart tap -> SKUs delivered to one client (counts + the
        #   revenue that adds back up to that client's bar),
        # - profit chart tap -> profit per SKU, then the cost chain.
        # Materials and print both come from the dynamic pass above â€” raw
        # materials at TODAY's FIFO queue prices, print at the CURRENT print
        # config (no frozen costs), so they always add up to the row's direct
        # cost and move together when a price or config is edited.
        matrix_draft = []
        for (client_id, sku_id), row in per_client_sku.items():
            matrix_draft.append(
                {
                    "client_id": client_id,
                    "client": row["client"],
                    "sku_id": sku_id,
                    "sku": row["sku"],
                    "cases": row["cases"],
                    "revenue": row["revenue"],
                    "direct_cost": row["direct_cost"],
                    "materials_cost": row["materials_cost"],
                    "print_cost": row["print_cost"],
                    "overhead": row["overhead"],
                }
            )

        # Overhead shares are rounded per SKU row; give the rounding remainder
        # of each client to its largest row so the drill-down rows add up to
        # the client bar to the paise (the bars keep their original formula).
        rows_by_client = {}
        for row in matrix_draft:
            rows_by_client.setdefault(row["client_id"], []).append(row)
        for rows in rows_by_client.values():
            target = money(sum((r["overhead"] for r in rows), Decimal("0")))
            rounded = [money(r["overhead"]) for r in rows]
            delta = target - sum(rounded, Decimal("0"))
            if delta:
                biggest = max(range(len(rows)), key=lambda i: rows[i]["overhead"])
                rows[biggest]["overhead"] = rounded[biggest] + delta

        sku_client_matrix = sorted(
            (
                {
                    "client_id": row["client_id"],
                    "client": row["client"],
                    "sku_id": row["sku_id"],
                    "sku": row["sku"],
                    "cases": dstr(row["cases"]),
                    "revenue": str(money(row["revenue"])),
                    "direct_cost": str(money(row["direct_cost"])),
                    "materials_cost": str(money(row["materials_cost"])),
                    "print_cost": str(money(row["print_cost"])),
                    "overhead": str(money(row["overhead"])),
                    "profit_excl_overhead": str(
                        money(row["revenue"] - row["direct_cost"])
                    ),
                    "profit_incl_overhead": str(
                        money(
                            row["revenue"]
                            - row["direct_cost"]
                            - row["overhead"]
                        )
                    ),
                }
                for row in matrix_draft
            ),
            key=lambda r: (r["client"], r["sku"]),
        )

        # Period overhead per category (feeds the profit drill-down cost
        # chain) â€” the sum across every month the period touches.
        overhead_categories = [
            {"category": r["category__name"], "amount": str(money(r["total"]))}
            for r in MonthlyOverhead.objects.filter(month__in=months)
            .values("category__name")
            .annotate(total=Sum("amount"))
            .order_by("category__name")
        ]

        # Month totals for the summary cards. Direct cost is recomputed
        # dynamically per delivery (today's FIFO queue + current print config);
        # overhead is the month's live bucket (the same rule the stock pages use).
        profit_excl_overhead = money(total_revenue - total_direct_cost)
        profit_incl_overhead = money(profit_excl_overhead - overhead_total)

        # Delivery register for the month (user request: a chronological list of
        # delivery date / client / SKU / qty, separate from the inward register).
        # Oldest first, with the id as the tie-break so two deliveries on the
        # same day keep their entry order.
        delivery_rows = [
            {
                "id": d.id,
                "date": d.date.isoformat(),
                "client_id": d.client_id,
                "client": d.client.name,
                "sku_id": d.sku_id,
                "sku": d.sku.description,
                "qty_cases": dstr(d.qty_cases),
                "selling_price_per_case": str(money(d.selling_price_per_case)),
                "total_amount": str(money(d.qty_cases * d.selling_price_per_case)),
                "stock_shortfall_flag": d.stock_shortfall_flag,
            }
            for d in sorted(month_deliveries, key=lambda x: (x.date, x.id))
        ]

        return Response(
            {
                "month": month,
                # Which period these figures cover. The UI uses it to label the
                # summary card ("Oct, 2026" vs "1 Jan â†’ 31 Mar 2026") and to
                # know that the month was ignored because dates were given.
                "period": {
                    "mode": period["mode"],
                    "month": month,
                    "from": (
                        period["from"].isoformat() if period["from"] else None
                    ),
                    "to": period["to"].isoformat() if period["to"] else None,
                },
                "stock": materials,
                # Orders pipeline for the Home screen's Pending / Ready to
                # Deliver panels. Sent with the dashboard so Home needs one
                # request per period instead of three.
                "orders": _orders_payload(),
                "stats": {
                    "cases_sold_per_sku": sold,
                    "client_breakdown": client_breakdown,
                    # Backwards-compatible alias: top 5 clients by revenue.
                    "top_clients": client_breakdown[:5],
                    "inward_materials": inward,
                    "deliveries": delivery_rows,
                    "total_cases": dstr(total_cases),
                    "total_revenue": str(money(total_revenue)),
                    "total_direct_cost": str(money(total_direct_cost)),
                    "total_overhead": str(money(overhead_total)),
                    "total_profit_excl_overhead": str(profit_excl_overhead),
                    "total_profit_incl_overhead": str(profit_incl_overhead),
                    "overhead_per_case": str(money(overhead_per_case_month)),
                    "sku_client_matrix": sku_client_matrix,
                    "overhead_categories": overhead_categories,
                },
            }
        )


class ReportView(APIView):
    """
    Reports/Stats (spec 4.7): per-SKU sold & inward quantities by
    day/week/month + rolling 30-day average. Query params:
      sku=<id> or sku=all (required), granularity=day|week|month, start, end
    Only sold/inward stats â€” no P&L, GST, or exports in v1 (spec 9).
    """

    def get(self, request):
        # sku=<id> for a single SKU, sku=all to aggregate every SKU
        # (user request: an "All SKUs" option in Reports).
        sku_param = request.query_params.get("sku")
        all_skus = sku_param == "all"
        sku = None if all_skus else SKU.objects.filter(pk=sku_param).first()
        if sku is None and not all_skus:
            return Response(
                {"detail": "sku query param is required (an sku id or 'all')"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        granularity = request.query_params.get("granularity", "day")
        if granularity not in ("day", "week", "month"):
            granularity = "day"

        deliveries = StockDelivery.objects.filter(is_deleted=False)
        if sku is not None:
            deliveries = deliveries.filter(sku=sku)
        deliveries = deliveries.order_by("date")
        start = request.query_params.get("start")
        end = request.query_params.get("end")
        if start:
            deliveries = deliveries.filter(date__gte=start)
        if end:
            deliveries = deliveries.filter(date__lte=end)

        # Bucket sold quantities by the chosen granularity.
        buckets = {}
        for d in deliveries:
            key = _bucket_key(d.date, granularity)
            b = buckets.setdefault(
                key, {"sold_cases": Decimal("0"), "revenue": Decimal("0")}
            )
            b["sold_cases"] += d.qty_cases
            b["revenue"] += d.qty_cases * d.selling_price_per_case

        # Inward: batch arrivals of the materials this SKU requires (every
        # material when sku=all), bucketed the same way so sold vs inward
        # sits side by side. qty_per_case is per-SKU knowledge, so it is
        # null in the all-SKUs view.
        inward_buckets = {}
        if sku is not None:
            material_rows = [
                (req.material, req.qty_per_case)
                for req in sku.requirements.select_related("material")
            ]
        else:
            material_rows = [(m, None) for m in Material.objects.all()]
        for material, qty_per_case in material_rows:
            batches = material.batches.filter(is_deleted=False)
            if start:
                batches = batches.filter(arrival_date__gte=start)
            if end:
                batches = batches.filter(arrival_date__lte=end)
            for b in batches:
                key = _bucket_key(b.arrival_date, granularity)
                slot = inward_buckets.setdefault(key, {})
                entry = slot.setdefault(
                    str(material.id),
                    {
                        "material_id": material.id,
                        "material": material.name,
                        "unit": material.unit_of_measure,
                        "quantity": Decimal("0"),
                        "qty_per_case": (
                            str(qty_per_case) if qty_per_case is not None else None
                        ),
                    },
                )
                entry["quantity"] += b.quantity_received

        series = sorted(set(list(buckets.keys()) + list(inward_buckets.keys())))
        rows = []
        for key in series:
            sold = buckets.get(
                key, {"sold_cases": Decimal("0"), "revenue": Decimal("0")}
            )
            inward_rows = [
                {
                    **{
                        k: (str(v) if isinstance(v, Decimal) else v)
                        for k, v in entry.items()
                    }
                }
                for entry in inward_buckets.get(key, {}).values()
            ]
            rows.append(
                {
                    "bucket": key,
                    "sold_cases": dstr(sold["sold_cases"]),
                    "revenue": str(money(sold["revenue"])),
                    "inward": inward_rows,
                }
            )

        # Rolling 30-day average (sold cases/day over the last 30 days).
        from datetime import timedelta

        today = timezone.now().date()
        window_start = today - timedelta(days=30)
        recent = StockDelivery.objects.filter(
            is_deleted=False, date__gte=window_start
        )
        if sku is not None:
            recent = recent.filter(sku=sku)
        recent_total = sum((d.qty_cases for d in recent), Decimal("0"))
        rolling = recent_total / Decimal("30")

        return Response(
            {
                "sku": (
                    {"id": sku.id, "description": sku.description}
                    if sku is not None
                    else {"id": None, "description": "All SKUs"}
                ),
                "granularity": granularity,
                "rows": rows,
                "rolling_30d_avg_cases_per_day": str(
                    rolling.quantize(Decimal("0.01"))
                ),
            }
        )


def _bucket_key(date_obj, granularity: str) -> str:
    if granularity == "day":
        return date_obj.strftime("%Y-%m-%d")
    if granularity == "week":
        iso = date_obj.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return date_obj.strftime("%Y-%m")


# ---------------------------------------------------------------------------
# App gate: password check + login-attempt audit (user request)
# ---------------------------------------------------------------------------
# 3 wrong passwords from one visitor in 300s restricts THAT visitor; 15 from
# anyone in the same window restricts everybody (brute-force backstop).
LOCKOUT_SECONDS = 300
LOCKOUT_THRESHOLD = 3
GLOBAL_LOCKOUT_THRESHOLD = 15
GEO_TIMEOUT_SECONDS = 2.0

# Best-effort geo cache, capped so a long-lived worker process cannot grow it
# without bound. Keyed by IP â€” a repeat visitor resolves instantly and the
# external service is not hammered by a lockout loop.
_GEO_CACHE = {}
_GEO_CACHE_MAX = 500

_PRIVATE_PREFIXES = ("10.", "192.168.", "127.", "169.254.", "::1", "fc", "fd")


def client_ip(request) -> str:
    """
    Best-effort visitor IP. Render/Netlify sit in front, so the real client
    address arrives in X-Forwarded-For (left-most hop is the original caller);
    REMOTE_ADDR is the fallback for direct/local requests.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.META.get("REMOTE_ADDR", "") or "")[:64]


def geo_lookup(ip: str) -> dict:
    """
    Best-effort location for an IP via the free `ipwho.is` HTTPS API.

    Hard rules: NEVER raises and NEVER blocks for long. A timeout, a DNS
    failure or a malformed reply all degrade to an IP-only row with
    geo_source="fallback" plus the error text, so the audit log explains WHY
    the location is blank instead of silently losing it.
    """
    empty = {
        "geo_country": "",
        "geo_region": "",
        "geo_city": "",
        "geo_isp": "",
        "geo_source": "fallback",
        "geo_error": "",
    }
    if not ip or ip.startswith(_PRIVATE_PREFIXES):
        return {
            **empty,
            "geo_source": "skipped",
            "geo_error": "private or empty address",
        }
    if ip in _GEO_CACHE:
        return dict(_GEO_CACHE[ip])

    payload = dict(empty)
    try:
        url = f"https://ipwho.is/{ip}"
        with urllib.request.urlopen(url, timeout=GEO_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        if body.get("success") is False:
            payload["geo_error"] = str(body.get("message") or "lookup refused")
        else:
            connection = body.get("connection") or {}
            payload.update(
                geo_country=str(body.get("country") or "")[:100],
                geo_region=str(body.get("region") or "")[:150],
                geo_city=str(body.get("city") or "")[:150],
                geo_isp=str(connection.get("isp") or "")[:200],
                geo_source="ok",
            )
    except Exception as exc:  # noqa: BLE001 - a geo failure must never break a login
        payload["geo_error"] = f"{type(exc).__name__}: {exc}"[:500]

    if len(_GEO_CACHE) < _GEO_CACHE_MAX:
        _GEO_CACHE[ip] = payload
    return dict(payload)


def _failure_count(ip=None, since=None):
    """
    Wrong-password attempts inside the sliding window. `locked_out=True` rows
    are excluded: they were refused before a password was ever compared, so
    counting them would let one visitor extend their own lockout forever.
    """
    qs = LoginAttempt.objects.filter(
        timestamp__gte=since, success=False, locked_out=False
    )
    if ip is not None:
        qs = qs.filter(ip=ip)
    return qs.count()


def _retry_after(ip, since):
    """
    Seconds until this visitor's oldest in-window failure ages out of the
    sliding window. Bounded by LOCKOUT_SECONDS so the answer is never
    "forever", and never 0 (they get at least one second).
    """
    oldest = (
        LoginAttempt.objects.filter(timestamp__gte=since, success=False, ip=ip)
        .order_by("timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    if not oldest:
        return LOCKOUT_SECONDS
    elapsed = (timezone.now() - oldest).total_seconds()
    return max(1, int(LOCKOUT_SECONDS - elapsed))


class PasswordView(APIView):
    """
    The app's front-door password check (user request: password "asdfghjkl",
    no way to change it from the UI, 3+ wrong attempts restrict that visitor
    for 300 seconds).

    Every attempt is stored in `LoginAttempt` with the visitor's IP, user
    agent and best-effort location â€” successes, failures and refused-while-
    locked attempts alike â€” readable at /loginattempts.

    Restriction is PER IP (a mistyping employee cannot lock out everybody),
    with a global backstop at 15 failures so a distributed brute force is
    still slowed down.

    NOTE: this guards the BROWSER UI only. The rest of the API has no
    authentication, so this is a soft gate plus an audit trail, not a
    security boundary.
    """

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        password = str(request.data.get("password") or "")
        ip = client_ip(request)
        since = timezone.now() - timedelta(seconds=LOCKOUT_SECONDS)
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")[:300]
        path = str(request.data.get("path") or "")[:200]

        ip_fails = _failure_count(ip, since)
        global_fails = _failure_count(None, since)

        if ip_fails >= LOCKOUT_THRESHOLD or global_fails >= GLOBAL_LOCKOUT_THRESHOLD:
            retry_after = _retry_after(ip, since)
            # Record the refused attempt too â€” a blocked visitor trying again
            # is exactly what the audit log should show.
            LoginAttempt.objects.create(
                ip=ip,
                user_agent=user_agent,
                x_forwarded_for=forwarded,
                success=False,
                locked_out=True,
                path=path,
                **geo_lookup(ip),
            )
            return Response(
                {
                    "ok": False,
                    "locked_out": True,
                    "retry_after": retry_after,
                    "detail": (
                        f"Access restricted after {LOCKOUT_THRESHOLD}+ wrong "
                        f"attempts. Try again in {retry_after} seconds."
                    ),
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        ok = password == settings.LOGIN_PASSWORD
        LoginAttempt.objects.create(
            ip=ip,
            user_agent=user_agent,
            x_forwarded_for=forwarded,
            success=ok,
            locked_out=False,
            path=path,
            **geo_lookup(ip),
        )
        if not ok:
            remaining = max(0, LOCKOUT_THRESHOLD - (ip_fails + 1))
            return Response(
                {
                    "ok": False,
                    "locked_out": False,
                    "attempts_remaining": remaining,
                    "detail": "Wrong password.",
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )
        return Response({"ok": True, "locked_out": False})


class ClientOrderViewSet(viewsets.ModelViewSet):
    """
    Orders pipeline (user request):

        Enter Order        -> PENDING    (reserves nothing)
        Mark complete      -> COMPLETED  (stock in hand -> Ready to Deliver)
        Mark delivered     -> DELIVERED  (a real StockDelivery per line)
        Delete a delivery  -> the line goes back to Ready to Deliver
        Move back to Pending -> PENDING  (Ready to Deliver -> stock in hand)

    The stock figures the whole flow depends on are DERIVED, never stored:
    `cogs.committed_map` sums the requirements of every COMPLETED/DELIVERED
    line that has no delivery yet, and `cogs.available_map` subtracts that
    from the live batch balance. So every transition above is correct by
    construction and cannot desync (see cogs.committed_map's docstring).

    Two different stock questions are asked along the way, and keeping them
    apart is what makes the numbers add up:
      - completing an order checks AVAILABLE stock (physical âˆ’ committed),
        excluding its own lines;
      - delivering it checks PHYSICAL stock, because FIFO consumes batches and
        the order's own commitment is exactly what it is about to consume.
    """

    # The Home and StatusUpdate screens render every order at once, so the
    # default page wrapper would hide rows behind a `results` key.
    pagination_class = None

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return ClientOrderWriteSerializer
        return ClientOrderSerializer

    def get_queryset(self):
        qs = ClientOrder.objects.select_related("client").prefetch_related(
            "items__sku", "items__delivery"
        )
        status_value = self.request.query_params.get("status")
        if status_value:
            qs = qs.filter(status=status_value)
        client_id = self.request.query_params.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)
        return qs.filter(is_deleted=False)

    @staticmethod
    def _order_payload(order):
        return ClientOrderSerializer(order).data

    def create(self, request, *args, **kwargs):
        serializer = ClientOrderWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = serializer.save()
        return Response(
            self._order_payload(order), status=status.HTTP_201_CREATED
        )

    def update(self, request, *args, **kwargs):
        order = self.get_object()
        if order.status == ClientOrder.DELIVERED:
            return Response(
                {
                    "detail": "This order has been delivered and can no longer "
                    "be edited."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = ClientOrderWriteSerializer(order, data=request.data)
        serializer.is_valid(raise_exception=True)
        order = serializer.save()
        return Response(self._order_payload(order))

    def partial_update(self, request, *args, **kwargs):
        # The write serializer replaces the line set wholesale, so PATCH would
        # silently drop lines that were not resent. Editing is all-or-nothing.
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        order = self.get_object()
        if order.status == ClientOrder.DELIVERED:
            return Response(
                {
                    "detail": "This order has deliveries against it â€” delete "
                    "those from the deliveries list first."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Soft delete: the row and its lines stay in the audit trail (spec 3.7).
        order.is_deleted = True
        order.save(skip_audit=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


    @action(detail=True, methods=["post"])
    def deliver(self, request, pk=None):
        """
        Deliver the ready lines: one StockDelivery per line, created through
        the SAME `cogs.create_delivery` the Add Delivery screen uses, so FIFO
        consumption, the ledger entry, the COGS snapshots and the shortfall
        check behave identically.

        All-or-nothing: a shortfall on any line rolls the whole batch back, so
        an order is never left half-delivered.
        """
        order = self.get_object()
        if order.status == ClientOrder.DELIVERED:
            return Response(
                {"detail": "This order has already been delivered."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order.status == ClientOrder.PENDING:
            return Response(
                {
                    "detail": "Move this order to Ready to Deliver before "
                    "delivering it."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        pending = order.undelivered_items
        if not pending:
            return Response(
                {"detail": "Nothing left to deliver on this order."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_date = request.data.get("date")
        delivery_date = None
        if raw_date:
            delivery_date = parse_date(str(raw_date))
            if delivery_date is None:
                return Response(
                    {"detail": "Delivery date is not a valid date."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        force = bool(request.data.get("force"))
        note = request.data.get("note") or f"Order #{order.id}"

        try:
            with transaction.atomic():
                made = []
                for item in pending:
                    delivery, _result = create_delivery(
                        client=order.client,
                        sku=item.sku,
                        qty_cases=item.qty_cases,
                        selling_price_per_case=item.selling_price_per_case,
                        delivery_date=delivery_date,
                        note=note,
                        force=force,
                    )
                    item.delivery = delivery
                    item.save()
                    made.append(delivery)
                order.delivered_at = timezone.now()
                order.recompute_status()
        except DeliveryShortfall as exc:
            # Nothing was committed: the atomic block rolled every delivery and
            # every ledger entry back, so the order is still COMPLETED. The
            # memo must be dropped too — its signals fired inside the rolled
            # back transaction (a stale generation survives it), so without
            # this the stock read below would still see the undone consumption.
            perfcache.invalidate()
            return Response(
                {
                    "detail": "Not enough stock to deliver this order.",
                    "shortages": exc.shortages,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )

        return Response(
            {
                "order": self._order_payload(order),
                "deliveries": [
                    {
                        "id": d.id,
                        "sku": d.sku.description,
                        "qty_cases": dstr(d.qty_cases),
                        "date": d.date.isoformat(),
                        "stock_shortfall_flag": d.stock_shortfall_flag,
                    }
                    for d in made
                ],
            }
        )

    # -- transitions ------------------------------------------------------
    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        """
        Move the order to Ready to Deliver: its material leaves Stock in Hand
        and appears in the Ready bucket.

        Judged against AVAILABLE stock (physical minus what other orders have
        already committed) and excluding this order's own lines, so an order
        can never be blocked by itself. `force=true` proceeds anyway â€” the
        resulting negative available figure is what the UI warns about.
        """
        order = self.get_object()
        if order.status != ClientOrder.PENDING:
            return Response(
                {
                    "detail": "Only a pending order can be moved to Ready to "
                    "Deliver."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        lines = [(i.sku, i.qty_cases) for i in order.items.all()]
        if not lines:
            return Response(
                {"detail": "This order has no items."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        exclude = list(order.items.values_list("id", flat=True))
        shortages = cogs.check_shortfall_many(
            order.client, lines, available_basis=True, exclude_item_ids=exclude
        )
        if shortages and not request.data.get("force"):
            return Response(
                {
                    "detail": "Not enough free stock for this order.",
                    "shortages": shortages,
                },
                status=status.HTTP_409_CONFLICT,
            )

        order.completed_at = timezone.now()
        order.recompute_status()
        return Response(self._order_payload(order))

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        """
        Move a Ready-to-Deliver order back to Pending: its material returns to
        Stock in Hand. Refused once anything has been delivered, because that
        stock has physically left.
        """
        order = self.get_object()
        if order.status == ClientOrder.DELIVERED:
            return Response(
                {"detail": "This order has deliveries against it."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order.status != ClientOrder.COMPLETED:
            return Response(
                {"detail": "This order is not in Ready to Deliver."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        order.completed_at = None
        order.status = ClientOrder.PENDING
        order.save(skip_audit=True)
        return Response(self._order_payload(order))


    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        One round trip for the StatusUpdate and Add Delivery screens: the
        pending orders, the Ready-to-Deliver bucket, and the per-material
        commitment behind them. Same payload the dashboard sends Home.
        """
        return Response(_orders_payload())


class LoginAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only audit trail behind the app gate. Attempts are written by
    PasswordView only â€” no create/edit/delete is exposed here. Newest first,
    with optional `?result=success|failed` and `?ip=` filters for the
    /loginattempts screen.
    """

    queryset = LoginAttempt.objects.all()
    pagination_class = None

    def get_queryset(self):
        qs = LoginAttempt.objects.all()
        result = self.request.query_params.get("result")
        if result == "success":
            qs = qs.filter(success=True)
        elif result == "failed":
            qs = qs.filter(success=False)
        ip = self.request.query_params.get("ip")
        if ip:
            qs = qs.filter(ip=ip)
        return qs

    def list(self, request, *args, **kwargs):
        rows = [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat(),
                "ip": r.ip,
                "user_agent": r.user_agent,
                "x_forwarded_for": r.x_forwarded_for,
                "success": r.success,
                "locked_out": r.locked_out,
                "geo_country": r.geo_country,
                "geo_region": r.geo_region,
                "geo_city": r.geo_city,
                "geo_isp": r.geo_isp,
                "geo_source": r.geo_source,
                "geo_error": r.geo_error,
                "path": r.path,
            }
            for r in self.get_queryset()[:500]
        ]
        return Response(rows)







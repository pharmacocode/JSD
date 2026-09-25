"""DRF views for the JSD Group API."""

from decimal import Decimal, InvalidOperation

from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from . import cogs
from .cogs import DeliveryShortfall, apply_stock_adjustment, create_delivery
from .models import (
    Client,
    ClientLedgerEntry,
    ClientSKUPrice,
    Employee,
    EmployeePayment,
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


class ClientViewSet(viewsets.ModelViewSet):
    queryset = Client.objects.all().prefetch_related("sku_prices__sku")
    serializer_class = ClientSerializer

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
        ordered = list(reversed(entries))
        serializer = ClientLedgerEntrySerializer(
            ordered,
            many=True,
            context={"balances": balances, "superseded": superseded},
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
        instance.soft_delete()  # never hard-delete (spec 3.7)

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
        DELETE -> clears the marker (payable = purchases − payments again).
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
        Cost breakup (spec 4.6 SKU detail) — PER CASE ONLY (bottles are never
        considered anywhere). Uses current FIFO batch prices + the live
        overhead allocation; historical deliveries keep their frozen
        cogs_per_case_snapshot.
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
                {"detail": "Create a client first — label stock resolves per client."},
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
    cogs_per_case_snapshot is set server-side only (frozen, spec 5).
    """

    queryset = StockDelivery.objects.all()
    serializer_class = StockDeliverySerializer

    def get_queryset(self):
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
            qs = qs.filter(date__year=year, date__month=mon)
        if not self.request.query_params.get("include_deleted"):
            qs = qs.filter(is_deleted=False)
        return qs

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
            )
        except DeliveryShortfall as exc:
            # Spec 4.2 step 4: clear warning payload -> Proceed/Cancel UI.
            return Response(
                {
                    "detail": (
                        "Insufficient stock — you can proceed and adjust "
                        "stock later, or cancel."
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
                    # Live figures (base frozen at creation + the delivery
                    # month's CURRENT overhead allocation).
                    "per_case": result["per_case_current"],
                    "per_case_at_creation": result["per_case"],
                    "base_per_case": result["base_per_case"],
                    "overhead_per_case_current": result["overhead_per_case_current"],
                    "details": result["details"],
                },
                "total_amount": result["total_amount"],
                "total_cogs_current": result["total_cogs_current"],
                "client_pending_amount": result["client_pending_amount"],
                "stock_shortfall_flag": delivery.stock_shortfall_flag,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"])
    def preview(self, request):
        """Non-mutating COGS + shortfall preview for the Add-Delivery screen."""
        from datetime import date as date_cls

        client = Client.objects.filter(pk=request.data.get("client")).first()
        sku = SKU.objects.filter(pk=request.data.get("sku")).first()
        if client is None or sku is None:
            return Response(
                {"detail": "client and sku are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qty_cases = Decimal(str(request.data.get("qty_cases", "1")))
        date_value = request.data.get("date")
        date_value = (
            date_cls.fromisoformat(date_value) if date_value else timezone.now().date()
        )
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
                "default_selling_price": (
                    str(csp.selling_price_per_case) if csp else None
                ),
            }
        )

    def perform_destroy(self, instance):
        instance.soft_delete()  # audit-trail delete (spec 3.7)


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
        Labour is never entered manually — it is always the sum of that
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


class DashboardView(APIView):
    """
    Home screen data (spec 4.1, figures reworked per user request):
    - stock in hand per material (counts + alert flags for the collapsed panel)
    - monthly summary for ?month=YYYY-MM: cases sold per SKU, per-client
      revenue / profit breakdown (drives the metric chart), inward material
      arrivals with their editable batch line items, overhead summary and the
      month's profit (with and without overhead).
    """

    def get(self, request):
        month = month_param(request)
        m_year, m_mon = (int(p) for p in month.split("-"))
        month_filter = {"date__year": m_year, "date__month": m_mon}

        # Stock in hand panel.
        materials = []
        for m in Material.objects.all().order_by("name"):
            materials.append(
                {
                    "id": m.id,
                    "name": m.name,
                    "category": m.category,
                    "unit": m.unit_of_measure,
                    "stock_in_hand": dstr(m.stock_in_hand),
                    "stock_alert_qty": dstr(m.stock_alert_qty),
                    "below_alert": m.is_below_alert,
                    "is_client_specific": m.is_client_specific,
                    "client": m.client_id,
                    "client_name": m.client.name if m.client else None,
                }
            )

        # Cases sold per SKU this month.
        sold = []
        for row in (
            StockDelivery.objects.filter(is_deleted=False, **month_filter)
            .values("sku_id", "sku__description")
            .annotate(cases=Sum("qty_cases"))
            .order_by("sku__description")
        ):
            deliveries = StockDelivery.objects.filter(
                is_deleted=False, **month_filter, sku_id=row["sku_id"]
            )
            revenue = sum(
                (d.qty_cases * d.selling_price_per_case for d in deliveries),
                Decimal("0"),
            )
            sold.append(
                {
                    "sku_id": row["sku_id"],
                    "sku": row["sku__description"],
                    "cases": dstr(row["cases"] or 0),
                    "revenue": str(money(revenue)),
                }
            )

        # Inward material arrivals this month: one row per material for the Home
        # summary, each carrying its underlying batch line items. Those line
        # items are editable from the Home screen (user request) — correcting
        # one recalculates stock in hand, FIFO and the vendor payable.
        arrivals = {}
        for batch in (
            MaterialBatch.objects.filter(
                is_deleted=False,
                arrival_date__year=m_year,
                arrival_date__month=m_mon,
            )
            .select_related("material")
            .order_by("material__name", "arrival_date", "id")
        ):
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
            inward.append(row)

        # Per-client breakdown this month: revenue, the frozen direct cost
        # (materials consumed + print) and profit with/without the month's
        # overhead. Powers the Home metric chart (revenue / profit per client,
        # highest first) — it replaces the old "top clients" doughnut.
        overhead_total = cogs.overhead_for_month(month)
        overhead_per_case_month = cogs.overhead_per_case(month)
        per_client = {}
        total_revenue = Decimal("0")
        total_direct_cost = Decimal("0")
        total_cases = Decimal("0")
        for d in StockDelivery.objects.filter(
            is_deleted=False, **month_filter
        ).select_related("client"):
            revenue = d.qty_cases * d.selling_price_per_case
            direct_cost = d.qty_cases * d.base_cogs_per_case_snapshot
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

        client_breakdown = []
        for client_id, row in per_client.items():
            # Overhead is a monthly bucket allocated per case sold (spec 6.3),
            # so each client carries its own share of the month's overhead.
            overhead = money(row["cases"] * overhead_per_case_month)
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

        # Month totals for the summary cards. Direct cost is frozen per delivery;
        # overhead is the month's live bucket (the same rule the stock pages use).
        profit_excl_overhead = money(total_revenue - total_direct_cost)
        profit_incl_overhead = money(profit_excl_overhead - overhead_total)

        return Response(
            {
                "month": month,
                "stock": materials,
                "stats": {
                    "cases_sold_per_sku": sold,
                    "client_breakdown": client_breakdown,
                    # Backwards-compatible alias: top 5 clients by revenue.
                    "top_clients": client_breakdown[:5],
                    "inward_materials": inward,
                    "total_cases": dstr(total_cases),
                    "total_revenue": str(money(total_revenue)),
                    "total_direct_cost": str(money(total_direct_cost)),
                    "total_overhead": str(money(overhead_total)),
                    "total_profit_excl_overhead": str(profit_excl_overhead),
                    "total_profit_incl_overhead": str(profit_incl_overhead),
                    "overhead_per_case": str(money(overhead_per_case_month)),
                },
            }
        )


class ReportView(APIView):
    """
    Reports/Stats (spec 4.7): per-SKU sold & inward quantities by
    day/week/month + rolling 30-day average. Query params:
      sku=<id> (required), granularity=day|week|month, start, end
    Only sold/inward stats — no P&L, GST, or exports in v1 (spec 9).
    """

    def get(self, request):
        sku = SKU.objects.filter(pk=request.query_params.get("sku")).first()
        if sku is None:
            return Response(
                {"detail": "sku query param is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        granularity = request.query_params.get("granularity", "day")
        if granularity not in ("day", "week", "month"):
            granularity = "day"

        deliveries = StockDelivery.objects.filter(
            is_deleted=False, sku=sku
        ).order_by("date")
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

        # Inward: batch arrivals of each material this SKU requires,
        # bucketed the same way so sold vs inward sits side by side.
        inward_buckets = {}
        for req in sku.requirements.select_related("material"):
            batches = req.material.batches.filter(is_deleted=False)
            if start:
                batches = batches.filter(arrival_date__gte=start)
            if end:
                batches = batches.filter(arrival_date__lte=end)
            for b in batches:
                key = _bucket_key(b.arrival_date, granularity)
                slot = inward_buckets.setdefault(key, {})
                entry = slot.setdefault(
                    str(req.material.id),
                    {
                        "material_id": req.material.id,
                        "material": req.material.name,
                        "unit": req.material.unit_of_measure,
                        "quantity": Decimal("0"),
                        "qty_per_case": str(req.qty_per_case),
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
            is_deleted=False, sku=sku, date__gte=window_start
        )
        recent_total = sum((d.qty_cases for d in recent), Decimal("0"))
        rolling = recent_total / Decimal("30")

        return Response(
            {
                "sku": {"id": sku.id, "description": sku.description},
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







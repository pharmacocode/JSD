"""DRF serializers for the JSD Group API."""

from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework import serializers

from .cogs import settle_deficit
from .models import (
    Client,
    ClientLedgerEntry,
    ClientOrder,
    ClientSKUPrice,
    Employee,
    EmployeePayment,
    Material,
    MaterialBatch,
    MonthlyOverhead,
    OrderItem,
    OverheadCategory,
    SKU,
    SKUMaterialRequirement,
    SKUPrintCost,
    StockAdjustment,
    StockDelivery,
    Vendor,
    VendorPayment,
    money,
)


class ClientSKUPriceSerializer(serializers.ModelSerializer):
    sku_description = serializers.CharField(source="sku.description", read_only=True)

    class Meta:
        model = ClientSKUPrice
        fields = [
            "id",
            "client",
            "sku",
            "sku_description",
            "selling_price_per_case",
        ]


class ClientLedgerEntrySerializer(serializers.ModelSerializer):
    running_balance = serializers.SerializerMethodField()
    running_balance_note = serializers.SerializerMethodField()
    delivery_ref = serializers.SerializerMethodField()
    # True when the client's pending "as of" marker supersedes this entry
    # (dated on/before the marked date, so already inside the entered figure).
    is_superseded = serializers.SerializerMethodField()

    class Meta:
        model = ClientLedgerEntry
        fields = [
            "id",
            "client",
            "entry_type",
            "amount",
            "related_delivery",
            "delivery_ref",
            "note",
            "date",
            "running_balance",
            "running_balance_note",
            "is_superseded",
            "is_edited",
            "is_deleted",
            "edit_history",
            "created_at",
        ]
        read_only_fields = ["is_edited", "is_deleted", "edit_history"]

    def get_is_superseded(self, obj):
        return obj.id in self.context.get("superseded", set())

    def get_delivery_ref(self, obj):
        if obj.related_delivery_id:
            return (
                f"{obj.related_delivery.sku.description} x"
                f"{obj.related_delivery.qty_cases}"
            )
        return None

    def get_running_balance(self, obj):
        # Computed in the ledger view (context["balances"] keyed by entry id);
        # omitted for plain list endpoints.
        return self.context.get("balances", {}).get(obj.id)

    def get_running_balance_note(self, obj):
        """
        Why a row has no running balance, or None. Deleted rows are excluded
        from the balance walk (they are not part of the live pending), so the
        UI can say so instead of silently showing "inside the as-of figure".
        """
        if self.context.get("deleted") and obj.id in self.context["deleted"]:
            return "Deleted — excluded from the pending amount"
        return None

    def validate_amount(self, value):
        entry_type = self.initial_data.get("entry_type")
        if entry_type == "PAYMENT" and value > 0:
            # Payments decrease pending — store negative (spec 3.2).
            return -value
        if entry_type in ("DELIVERY", "OPENING_BALANCE") and value < 0:
            return -value
        return value


class ClientSerializer(serializers.ModelSerializer):
    pending_amount = serializers.SerializerMethodField()
    sku_prices = ClientSKUPriceSerializer(many=True, read_only=True)

    class Meta:
        model = Client
        fields = [
            "id",
            "name",
            "google_maps_url",
            "contact_number",
            "opening_pending_amount",
            # Pending marker — editable at any time via the client detail screen
            # (POST/DELETE /clients/<id>/pending/) or a plain PATCH.
            "pending_as_of_amount",
            "pending_as_of_date",
            "created_at",
            "pending_amount",
            "sku_prices",
        ]

    def create(self, validated_data):
        # Opening balance ledger entry is created by Client.save() (model
        # level) so the live pending sum works from day one (spec 3.1/3.2).
        return super().create(validated_data)

    def get_pending_amount(self, obj):
        # Batch path (the client LIST): the viewset precomputed every pending
        # in ONE ledger query and stashed the map on the context. Without it
        # (retrieve/update) fall back to the per-client property — same number
        # either way.
        pending_map = self.context.get("pending_map")
        if pending_map is not None and obj.id in pending_map:
            return pending_map[obj.id]
        return str(obj.pending_amount)


class MaterialSerializer(serializers.ModelSerializer):
    stock_in_hand = serializers.DecimalField(
        max_digits=14, decimal_places=4, read_only=True
    )
    is_below_alert = serializers.BooleanField(read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)

    class Meta:
        model = Material
        fields = [
            "id",
            "name",
            "category",
            "is_client_specific",
            "client",
            "client_name",
            "vendor",
            "vendor_name",
            "current_price_per_unit",
            "stock_alert_qty",
            "wastage_percent",
            "unit_of_measure",
            "stock_in_hand",
            "is_below_alert",
            "created_at",
        ]


class VendorSerializer(serializers.ModelSerializer):
    """Vendor list entry: name, contact number, amount owed (read-only)."""

    amount_owed = serializers.SerializerMethodField()
    total_purchased = serializers.SerializerMethodField()
    total_paid = serializers.SerializerMethodField()
    material_count = serializers.IntegerField(source="materials.count", read_only=True)

    class Meta:
        model = Vendor
        fields = [
            "id",
            "name",
            "contact_number",
            # Payable marker — editable at any time via the vendor detail screen
            # (POST/DELETE /vendors/<id>/payable/) or a plain PATCH.
            "payable_as_of_amount",
            "payable_as_of_date",
            "amount_owed",
            "total_purchased",
            "total_paid",
            "material_count",
            "created_at",
        ]

    def get_amount_owed(self, obj):
        return str(obj.amount_owed)

    def get_total_purchased(self, obj):
        return str(obj.total_purchased)

    def get_total_paid(self, obj):
        return str(obj.total_paid)


class VendorPaymentSerializer(serializers.ModelSerializer):
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)

    class Meta:
        model = VendorPayment
        fields = [
            "id",
            "vendor",
            "vendor_name",
            "amount",
            "date",
            "note",
            "is_deleted",
            "created_at",
        ]


class MaterialBatchSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)
    material_unit = serializers.CharField(
        source="material.unit_of_measure", read_only=True
    )
    # Cases already consumed by this batch's FIFO walk (received − remaining).
    # Read-only: it follows whatever `quantity_received` is edited to.
    consumed = serializers.DecimalField(
        source="consumed_quantity", max_digits=12, decimal_places=4, read_only=True
    )

    class Meta:
        model = MaterialBatch
        fields = [
            "id",
            "material",
            "material_name",
            "material_unit",
            "quantity_received",
            "quantity_remaining",
            "consumed",
            "price_per_unit",
            "arrival_date",
            "note",
            "is_edited",
            "is_deleted",
            "edit_history",
            "created_at",
        ]
        read_only_fields = [
            "quantity_remaining",
            "is_edited",
            "is_deleted",
            "edit_history",
        ]

    def create(self, validated_data):
        # Arrived-stock entry (spec 4.3): remaining starts = received.
        validated_data.setdefault(
            "quantity_remaining", validated_data["quantity_received"]
        )
        batch = super().create(validated_data)
        # Stock arriving after a delivery was already made against it settles
        # that deficit, instead of leaving the shortage standing next to the very
        # stock that covers it (user report: batch history showed a permanent
        # -170 beside an arrival of 200 that had in fact been delivered).
        settle_deficit(batch.material, batch)
        return batch


class SKUMaterialRequirementSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)
    material_unit = serializers.CharField(
        source="material.unit_of_measure", read_only=True
    )

    class Meta:
        model = SKUMaterialRequirement
        fields = [
            "id",
            "sku",
            "material",
            "material_name",
            "material_unit",
            "qty_per_case",
        ]

    def validate_qty_per_case(self, value):
        """
        A requirement of 0 (or less) per case silently consumes nothing — the
        SKU ships, stock never moves and no shortfall can ever be reported (or,
        when negative, stock would be ADDED). Rejected up front.
        """
        if value <= 0:
            raise serializers.ValidationError(
                "Quantity per case must be greater than zero."
            )
        return value


class SKUPrintCostSerializer(serializers.ModelSerializer):
    class Meta:
        model = SKUPrintCost
        fields = [
            "id",
            "sku",
            "paper_cost",
            "print_cost_per_paper",
            "labels_per_paper",
            "wastage_percent",
        ]


class SKUSerializer(serializers.ModelSerializer):
    requirements = SKUMaterialRequirementSerializer(many=True, read_only=True)
    print_cost = SKUPrintCostSerializer(read_only=True)

    class Meta:
        model = SKU
        fields = [
            "id",
            "description",
            "qty_per_case",
            "volume_ml",
            "created_at",
            "requirements",
            "print_cost",
        ]


class StockDeliverySerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    total_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    total_cogs = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    # Current direct cost (materials + print) — recomputed from today's FIFO
    # queue prices and the current print config (no frozen costs; the
    # base_cogs_per_case_snapshot field stays in the audit trail only).
    base_cogs_per_case = serializers.DecimalField(
        source="base_cogs_per_case_current",
        max_digits=12,
        decimal_places=2,
        read_only=True,
    )
    # Dynamic parts: the delivery month's CURRENT overhead per case and the
    # resulting live COGS (both dynamic — nothing is frozen).
    overhead_per_case_current = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )
    cogs_per_case_current = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = StockDelivery
        fields = [
            "id",
            "client",
            "client_name",
            "sku",
            "sku_description",
            "date",
            "qty_cases",
            "selling_price_per_case",
            "cogs_per_case_snapshot",
            "base_cogs_per_case",
            "overhead_per_case_current",
            "cogs_per_case_current",
            "stock_shortfall_flag",
            "total_amount",
            "total_cogs",
            "is_edited",
            "is_deleted",
            "edit_history",
            "created_at",
        ]
        read_only_fields = [
            "base_cogs_per_case_snapshot",
            "cogs_per_case_snapshot",
            "stock_shortfall_flag",
            "is_edited",
            "is_deleted",
            "edit_history",
        ]


class StockAdjustmentSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)

    class Meta:
        model = StockAdjustment
        fields = [
            "id",
            "material",
            "material_name",
            "quantity",
            "reason",
            "date",
            "reference_batches",
            "is_edited",
            "is_deleted",
            "edit_history",
            "created_at",
        ]
        read_only_fields = [
            "reference_batches",
            "is_edited",
            "is_deleted",
            "edit_history",
        ]

    def validate_reason(self, value):
        if not str(value).strip():
            raise serializers.ValidationError("A reason is required.")
        return value


class OverheadCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = OverheadCategory
        fields = ["id", "name", "is_default"]


class MonthlyOverheadSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = MonthlyOverhead
        fields = [
            "id",
            "category",
            "category_name",
            "month",
            "amount",
        ]

class OrderItemSerializer(serializers.ModelSerializer):
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    line_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    delivery_date = serializers.DateField(
        source="delivery.date", read_only=True, default=None
    )

    class Meta:
        model = OrderItem
        fields = [
            "id",
            "sku",
            "sku_description",
            "qty_cases",
            "selling_price_per_case",
            "line_amount",
            "delivery",
            "delivery_date",
        ]

    def validate(self, attrs):
        qty = attrs.get("qty_cases", getattr(self.instance, "qty_cases", None))
        if qty is not None and qty <= 0:
            raise serializers.ValidationError(
                {"qty_cases": "Quantity must be greater than zero."}
            )
        price = attrs.get(
            "selling_price_per_case",
            getattr(self.instance, "selling_price_per_case", None),
        )
        if price is not None and price <= 0:
            raise serializers.ValidationError(
                {"selling_price_per_case": "Price must be greater than zero."}
            )
        return attrs


class ClientOrderSerializer(serializers.ModelSerializer):
    """
    Read shape: the order with its lines nested, plus the totals the Home /
    StatusUpdate screens show.

    `status` is derived from the lines (see ClientOrder.recompute_status), so
    it is read-only here — an order reaches DELIVERED by its lines getting
    deliveries, not by someone typing a status.
    """

    client_name = serializers.CharField(source="client.name", read_only=True)
    items = OrderItemSerializer(many=True, read_only=True)
    total_qty = serializers.DecimalField(
        max_digits=14, decimal_places=4, read_only=True
    )
    total_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    status_display = serializers.CharField(
        source="get_status_display", read_only=True
    )

    class Meta:
        model = ClientOrder
        fields = [
            "id",
            "client",
            "client_name",
            "order_date",
            "status",
            "status_display",
            "completed_at",
            "delivered_at",
            "notes",
            "items",
            "total_qty",
            "total_amount",
            "is_deleted",
            "created_at",
        ]
        read_only_fields = ["status", "completed_at", "delivered_at", "is_deleted"]


class ClientOrderWriteSerializer(serializers.Serializer):
    """
    Write shape for creating and editing an order. The line set is REPLACED
    wholesale on edit (same contract as the multi-line delivery endpoint), so
    a partial edit can never leave a stale line behind.

    Prices are optional per line: blank falls back to the client's configured
    ClientSKUPrice, which is the "pre-filled, editable" behaviour the Enter
    Order modal needs.
    """

    client = serializers.PrimaryKeyRelatedField(queryset=Client.objects.all())
    order_date = serializers.DateField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    items = serializers.ListField(allow_empty=False)

    def validate_items(self, lines):
        cleaned = []
        seen = set()
        for line in lines:
            sku_id = line.get("sku")
            if sku_id in seen:
                raise serializers.ValidationError(
                    f"SKU {sku_id} appears twice on this order — combine the "
                    "quantities into one line."
                )
            seen.add(sku_id)
            try:
                qty = Decimal(str(line.get("qty_cases")))
            except (TypeError, InvalidOperation):
                raise serializers.ValidationError(
                    f"Quantity is required for SKU {sku_id}."
                )
            if qty <= 0:
                raise serializers.ValidationError(
                    f"Quantity must be greater than zero for SKU {sku_id}."
                )
            raw_price = line.get("selling_price_per_case")
            price = None
            if raw_price not in (None, ""):
                try:
                    price = Decimal(str(raw_price))
                except (TypeError, InvalidOperation):
                    raise serializers.ValidationError(
                        f"Price is not a number for SKU {sku_id}."
                    )
                if price <= 0:
                    raise serializers.ValidationError(
                        f"Price must be greater than zero for SKU {sku_id}."
                    )
            cleaned.append({"sku_id": sku_id, "qty_cases": qty, "price": price})
        return cleaned

    def _price_for(self, client, sku_id):
        """
        The order-time price: the client's configured ClientSKUPrice — the same
        source the Enter Order modal pre-fills from. There is deliberately no
        SKU-level fallback: an order is a promise of money, so a pair with no
        agreed price must be filled in rather than guessed at.
        """
        csp = ClientSKUPrice.objects.filter(client=client, sku_id=sku_id).first()
        if csp is not None:
            return money(csp.selling_price_per_case)
        return None

    def _resolve_prices(self, client, lines):
        """
        Blank prices fall back to the configured one. Raises if neither is
        available, so an order is never saved with a zero/None price that
        would silently zero the client's ledger when delivered.
        """
        resolved = []
        for line in lines:
            price = line["price"] or self._price_for(client, line["sku_id"])
            if price is None:
                raise serializers.ValidationError(
                    {
                        "items": "No selling price configured for one of these "
                        "SKUs — enter a price."
                    }
                )
            resolved.append({**line, "price": price})
        return resolved

    def create(self, validated_data):
        from django.db import transaction

        lines = self._resolve_prices(
            validated_data["client"], validated_data["items"]
        )
        with transaction.atomic():
            order = ClientOrder.objects.create(
                client=validated_data["client"],
                order_date=validated_data.get("order_date")
                or timezone.now().date(),
                notes=validated_data.get("notes", ""),
            )
            for line in lines:
                OrderItem.objects.create(
                    order=order,
                    sku_id=line["sku_id"],
                    qty_cases=line["qty_cases"],
                    selling_price_per_case=line["price"],
                )
            order.recompute_status()
        return order

    def update(self, instance, validated_data):
        from django.db import transaction

        lines = self._resolve_prices(
            validated_data.get("client", instance.client),
            validated_data["items"],
        )
        with transaction.atomic():
            instance.client = validated_data.get("client", instance.client)
            if "order_date" in validated_data:
                instance.order_date = validated_data["order_date"]
            instance.notes = validated_data.get("notes", instance.notes)
            instance.save()

            # Replace the undelivered line set. A line that already produced a
            # delivery keeps its link — that stock really left the warehouse,
            # so an edit must not orphan its delivery.
            delivered = {
                i.sku_id: i
                for i in instance.items.filter(delivery__isnull=False)
            }
            instance.items.filter(delivery__isnull=True).delete()
            for line in lines:
                existing = delivered.get(line["sku_id"])
                if existing is not None:
                    existing.qty_cases = line["qty_cases"]
                    existing.selling_price_per_case = line["price"]
                    existing.save()
                    continue
                OrderItem.objects.create(
                    order=instance,
                    sku_id=line["sku_id"],
                    qty_cases=line["qty_cases"],
                    selling_price_per_case=line["price"],
                )
            instance.recompute_status()
        return instance


class EmployeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = ["id", "name", "role", "monthly_pay", "active", "created_at"]

class EmployeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = ["id", "name", "role", "monthly_pay", "active", "created_at"]


class EmployeePaymentSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.name", read_only=True)

    class Meta:
        model = EmployeePayment
        fields = [
            "id",
            "employee",
            "employee_name",
            "month",
            "amount_paid",
            "date",
            "note",
            "is_deleted",
            "created_at",
        ]


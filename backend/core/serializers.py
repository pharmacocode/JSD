"""DRF serializers for the JSD Group API."""

from rest_framework import serializers

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

    def validate_amount(self, value):
        entry_type = self.initial_data.get("entry_type")
        if entry_type == "PAYMENT" and value > 0:
            # Payments decrease pending — store negative (spec 3.2).
            return -value
        if entry_type in ("DELIVERY", "OPENING_BALANCE") and value < 0:
            return -value
        return value


class ClientSerializer(serializers.ModelSerializer):
    pending_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )
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
        return super().create(validated_data)


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


from django.contrib import admin

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


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_number", "opening_pending_amount", "created_at")
    search_fields = ("name",)


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "category",
        "is_client_specific",
        "client",
        "vendor",
        "stock_alert_qty",
    )
    list_filter = ("category", "is_client_specific")


@admin.register(MaterialBatch)
class MaterialBatchAdmin(admin.ModelAdmin):
    list_display = (
        "material",
        "quantity_received",
        "quantity_remaining",
        "price_per_unit",
        "arrival_date",
    )


@admin.register(SKU)
class SKUAdmin(admin.ModelAdmin):
    list_display = ("description", "qty_per_case", "volume_ml")


class ClientSKUPriceInline(admin.TabularInline):
    model = ClientSKUPrice
    extra = 1


class RequirementInline(admin.TabularInline):
    model = SKUMaterialRequirement
    extra = 1


admin.site.register(ClientSKUPrice)
admin.site.register(ClientLedgerEntry)
admin.site.register(SKUMaterialRequirement)
admin.site.register(SKUPrintCost)


@admin.register(StockDelivery)
class StockDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "client",
        "sku",
        "qty_cases",
        "selling_price_per_case",
        "base_cogs_per_case_snapshot",
        "cogs_per_case_snapshot",
        "stock_shortfall_flag",
    )
    list_filter = ("stock_shortfall_flag",)


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_number", "amount_owed", "created_at")
    search_fields = ("name", "contact_number")


admin.site.register(VendorPayment)
admin.site.register(StockAdjustment)
admin.site.register(OverheadCategory)
admin.site.register(MonthlyOverhead)
admin.site.register(Employee)
admin.site.register(EmployeePayment)

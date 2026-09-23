"""API URL routing — /api/..."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ClientLedgerEntryViewSet,
    ClientSKUPriceViewSet,
    ClientViewSet,
    DashboardView,
    EmployeePaymentViewSet,
    EmployeeViewSet,
    MaterialBatchViewSet,
    MaterialViewSet,
    MonthlyOverheadViewSet,
    OverheadCategoryViewSet,
    ReportView,
    SKUViewSet,
    SKUMaterialRequirementViewSet,
    StockAdjustmentViewSet,
    StockDeliveryViewSet,
    VendorPaymentViewSet,
    VendorViewSet,
)

router = DefaultRouter()
router.register(r"clients", ClientViewSet, basename="client")
router.register(r"client-prices", ClientSKUPriceViewSet, basename="client-price")
router.register(r"ledger", ClientLedgerEntryViewSet, basename="ledger")
router.register(r"materials", MaterialViewSet, basename="material")
router.register(r"batches", MaterialBatchViewSet, basename="batch")
router.register(r"vendors", VendorViewSet, basename="vendor")
router.register(r"vendor-payments", VendorPaymentViewSet, basename="vendor-payment")
router.register(r"skus", SKUViewSet, basename="sku")
router.register(
    r"sku-requirements", SKUMaterialRequirementViewSet, basename="sku-requirement"
)
router.register(r"deliveries", StockDeliveryViewSet, basename="delivery")
router.register(r"adjustments", StockAdjustmentViewSet, basename="adjustment")
router.register(
    r"overhead-categories", OverheadCategoryViewSet, basename="overhead-category"
)
router.register(r"overheads", MonthlyOverheadViewSet, basename="overhead")
router.register(r"employees", EmployeeViewSet, basename="employee")
router.register(
    r"employee-payments", EmployeePaymentViewSet, basename="employee-payment"
)

urlpatterns = [
    path("", include(router.urls)),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("reports/", ReportView.as_view(), name="reports"),
]

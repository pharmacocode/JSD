"""API URL routing — /api/..."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AssetViewSet,
    ClientLedgerEntryViewSet,
    ClientOrderViewSet,
    ClientSKUPriceViewSet,
    ClientViewSet,
    DashboardView,
    EmployeePaymentViewSet,
    EmployeeViewSet,
    LoanViewSet,
    LoginAttemptViewSet,
    MaterialBatchViewSet,
    MaterialViewSet,
    MonthlyOverheadViewSet,
    NetWorthView,
    OverheadCategoryViewSet,
    PasswordView,
    PingView,
    ReportView,
    SKUViewSet,
    SKUMaterialRequirementViewSet,
    StockAdjustmentViewSet,
    StockDeliveryViewSet,
    VendorPaymentViewSet,
    VendorViewSet,
)

router = DefaultRouter()
router.register(r"assets", AssetViewSet, basename="asset")
router.register(r"loans", LoanViewSet, basename="loan")
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
router.register(
    r"login-attempts", LoginAttemptViewSet, basename="login-attempt"
)
# Orders pipeline: Enter Order -> Pending -> Ready to Deliver -> Delivered.
router.register(r"orders", ClientOrderViewSet, basename="order")

urlpatterns = [
    path("", include(router.urls)),
    path("ping/", PingView.as_view(), name="ping"),
    # App gate (user request): password check + the audit list it feeds.
    path("auth/password/", PasswordView.as_view(), name="auth-password"),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    # Balance sheet behind the Home tiles (user request).
    path("networth/", NetWorthView.as_view(), name="networth"),
    path("reports/", ReportView.as_view(), name="reports"),
]

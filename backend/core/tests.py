"""
Unit tests for the FIFO costing engine + COGS behavior (spec Sections 5-6),
audit rules (3.7), ledger integrity (3.2), and API flows (4.2).
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from .cogs import (
    DeliveryShortfall,
    apply_stock_adjustment,
    check_shortfall,
    consume_fifo,
    create_delivery,
)
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
    StockDelivery,
    Vendor,
    VendorPayment,
    money,
)

D = Decimal


def make_material(name="Bottle 500ml", price="10", **kwargs):
    return Material.objects.create(
        name=name,
        category=kwargs.pop("category", "Bottle"),
        current_price_per_unit=D(price),
        **kwargs,
    )


def make_batch(material, qty, price, arrival=date(2026, 8, 1)):
    return MaterialBatch.objects.create(
        material=material,
        quantity_received=D(qty),
        quantity_remaining=D(qty),
        price_per_unit=D(price),
        arrival_date=arrival,
    )


class FIFOConsumptionTests(TestCase):
    """Spec 5: consume oldest batch first, roll over to next-oldest."""

    def setUp(self):
        self.mat = make_material()
        self.aug = make_batch(self.mat, "100", "10", arrival=date(2026, 8, 1))
        self.sep = make_batch(self.mat, "100", "12", arrival=date(2026, 9, 1))

    def test_consumes_oldest_batch_first(self):
        consumption, cost, shortfall = consume_fifo(self.mat, D("60"))
        self.assertEqual(len(consumption), 1)
        self.assertEqual(consumption[0]["batch_id"], self.aug.id)
        self.assertEqual(cost, D("600"))
        self.assertEqual(shortfall, D("0"))
        self.aug.refresh_from_db()
        self.sep.refresh_from_db()
        self.assertEqual(self.aug.quantity_remaining, D("40"))
        self.assertEqual(self.sep.quantity_remaining, D("100"))

    def test_rolls_over_to_next_batch(self):
        """Consume across the boundary: 100 from Aug + 50 from Sep."""
        consumption, cost, shortfall = consume_fifo(self.mat, D("150"))
        self.assertEqual(len(consumption), 2)
        self.assertEqual(cost, D("1000") + D("600"))  # 100x10 + 50x12
        self.assertEqual(shortfall, D("0"))
        self.aug.refresh_from_db()
        self.sep.refresh_from_db()
        self.assertEqual(self.aug.quantity_remaining, D("0"))
        self.assertEqual(self.sep.quantity_remaining, D("50"))

    def test_cost_follows_batch_not_calendar_month(self):
        """
        Spec 5 headline case: a SEPTEMBER consumption still carries August
        pricing while August stock remains in the queue.
        """
        consume_fifo(self.mat, D("30"))  # leaves 70 in the August batch
        consumption, cost, _ = consume_fifo(self.mat, D("50"))
        self.assertTrue(all(D(c["price_per_unit"]) == D("10") for c in consumption))
        self.assertEqual(cost, D("500"))

    def test_shortfall_without_negative_allowed(self):
        _c, _cost, shortfall = consume_fifo(
            self.mat, D("250"), allow_negative=False
        )
        self.assertEqual(shortfall, D("50"))
        for b in self.mat.batches.all():
            self.assertGreaterEqual(b.quantity_remaining, D("0"))

    def test_negative_remaining_when_forced(self):
        consume_fifo(self.mat, D("250"), allow_negative=True)
        total = sum(
            (b.quantity_remaining for b in self.mat.batches.all()), D("0")
        )
        self.assertEqual(total, D("-50"))  # reconcilable via adjustment later


class COGSFrozenSnapshotTests(TestCase):
    """Spec 5/6: COGS snapshot frozen at delivery creation."""

    def setUp(self):
        self.client_obj = Client.objects.create(name="Alpha Traders")
        self.sku = SKU.objects.create(
            description="500ml Mineral", qty_per_case=D("24"), volume_ml=500
        )
        self.bottle = make_material("Bottle", "24")
        self.label = make_material(
            "Label Alpha",
            category="Label",
            is_client_specific=True,
            client=self.client_obj,
            price="12",
        )
        SKUMaterialRequirement.objects.create(
            sku=self.sku, material=self.bottle, qty_per_case=D("1")
        )
        SKUMaterialRequirement.objects.create(
            sku=self.sku, material=self.label, qty_per_case=D("1")
        )
        SKUPrintCost.objects.create(
            sku=self.sku,
            paper_cost=D("50"),
            print_cost_per_paper=D("10"),
            labels_per_paper=D("100"),
            wastage_percent=D("5"),
        )
        # Stock is in CASES (bottles are never considered).
        make_batch(self.bottle, "1000", "24")
        make_batch(self.label, "1000", "12")
        ClientSKUPrice.objects.create(
            client=self.client_obj, sku=self.sku, selling_price_per_case=D("300")
        )
        rent = OverheadCategory.objects.get(name="Rent")
        MonthlyOverhead.objects.create(category=rent, month="2026-09", amount=D("480"))

    def expected_cogs_per_case(self, qty_cases):
        # Per case only: raw materials (1 case bottle + 1 case label) + print
        # for 24 labels + overhead per case.
        raw_per_case = D("24") + D("12")
        print_per_case = (D("50") + D("10")) / D("100") * D("24") * D("1.05")
        oh_per_case = D("480") / D(qty_cases)
        return money(raw_per_case + print_per_case + oh_per_case)

    def test_cogs_snapshot_value(self):
        delivery, _result = create_delivery(
            self.client_obj, self.sku, D("10"), delivery_date=date(2026, 9, 10)
        )
        self.assertEqual(
            delivery.cogs_per_case_snapshot,
            self.expected_cogs_per_case("10"),
        )

    def test_snapshot_immutable_against_future_changes(self):
        """Direct (base) cost never alters; overhead stays dynamic per month."""
        delivery, _result = create_delivery(
            self.client_obj, self.sku, D("10"), delivery_date=date(2026, 9, 10)
        )
        frozen_base = delivery.base_cogs_per_case_snapshot

        # New batch at a wildly different price per case.
        make_batch(self.bottle, "500", "999", arrival=date(2026, 9, 15))
        # Overhead changes for the month — live allocation moves, base stays.
        rent = OverheadCategory.objects.get(name="Rent")
        MonthlyOverhead.objects.filter(category=rent).update(amount=D("99999"))
        # Master display price changes.
        Material.objects.filter(pk=self.bottle.pk).update(
            current_price_per_unit=D("1")
        )

        delivery.refresh_from_db()
        self.assertEqual(delivery.base_cogs_per_case_snapshot, frozen_base)
        self.assertEqual(
            delivery.cogs_per_case_current,
            money(frozen_base + delivery.overhead_per_case_current),
        )

    def test_second_delivery_in_month_uses_live_overhead_estimate(self):
        """Overhead-per-case recomputes with month-to-date sales (assumption 1)."""
        create_delivery(
            self.client_obj, self.sku, D("10"), delivery_date=date(2026, 9, 5)
        )
        second, _ = create_delivery(
            self.client_obj, self.sku, D("14"), delivery_date=date(2026, 9, 20)
        )
        # Now 24 cases sold in Sept: overhead per case = 480/24 = 20.
        self.assertEqual(
            second.cogs_per_case_snapshot,
            self.expected_cogs_per_case("24"),
        )


class DeliveryFlowTests(TestCase):
    """Spec 4.2: shortfall check, force proceed, ledger linkage."""

    def setUp(self):
        self.client_obj = Client.objects.create(name="Beta Corp")
        self.sku = SKU.objects.create(
            description="500ml", qty_per_case=D("24"), volume_ml=500
        )
        self.bottle = make_material("Bottle", "240")
        SKUMaterialRequirement.objects.create(
            sku=self.sku, material=self.bottle, qty_per_case=D("1")
        )
        make_batch(self.bottle, "10", "240")  # exactly 10 cases in stock
        ClientSKUPrice.objects.create(
            client=self.client_obj, sku=self.sku, selling_price_per_case=D("300")
        )

    def test_insufficient_stock_raises_shortfall_without_force(self):
        shortages = check_shortfall(self.sku, self.client_obj, D("20"))
        self.assertEqual(len(shortages), 1)
        self.assertEqual(D(shortages[0]["short_by"]), D("10"))
        with self.assertRaises(DeliveryShortfall):
            create_delivery(
                self.client_obj, self.sku, D("20"), delivery_date=date(2026, 9, 1)
            )
        # Nothing was created or consumed (atomic rollback path).
        self.assertEqual(StockDelivery.objects.count(), 0)
        self.assertEqual(
            self.bottle.batches.first().quantity_remaining, D("10")
        )

    def test_force_proceed_sets_shortfall_flag_and_goes_negative(self):
        delivery, _result = create_delivery(
            self.client_obj,
            self.sku,
            D("20"),
            delivery_date=date(2026, 9, 1),
            force=True,
        )
        self.assertTrue(delivery.stock_shortfall_flag)
        total = sum(
            (b.quantity_remaining for b in self.bottle.batches.all()), D("0")
        )
        self.assertEqual(total, D("10") - D("20"))  # 10 cases short

    def test_delivery_creates_linked_ledger_entry(self):
        delivery, result = create_delivery(
            self.client_obj, self.sku, D("5"), delivery_date=date(2026, 9, 1)
        )
        entry = ClientLedgerEntry.objects.get(related_delivery=delivery)
        self.assertEqual(entry.entry_type, "DELIVERY")
        self.assertEqual(entry.amount, D("1500"))  # 5 x 300
        self.assertEqual(self.client_obj.pending_amount, D("1500"))
        self.assertEqual(result["client_pending_amount"], "1500.00")


class LedgerAndAuditTests(TestCase):
    """Spec 3.2 (live pending balance) + 3.7 (soft delete / edit history)."""

    def setUp(self):
        self.client_obj = Client.objects.create(
            name="Gamma Ltd", opening_pending_amount=D("5000")
        )

    def test_opening_balance_creates_ledger_entry(self):
        entry = self.client_obj.ledger_entries.get()
        self.assertEqual(entry.entry_type, "OPENING_BALANCE")
        self.assertEqual(entry.amount, D("5000"))
        self.assertEqual(self.client_obj.pending_amount, D("5000"))

    def test_payment_decreases_pending(self):
        ClientLedgerEntry.objects.create(
            client=self.client_obj, entry_type="DELIVERY", amount=D("1500")
        )
        ClientLedgerEntry.objects.create(
            client=self.client_obj, entry_type="PAYMENT", amount=D("-2000")
        )
        self.assertEqual(self.client_obj.pending_amount, D("4500"))

    def test_soft_delete_excluded_from_balance_but_row_survives(self):
        e = ClientLedgerEntry.objects.create(
            client=self.client_obj, entry_type="DELIVERY", amount=D("100")
        )
        e.soft_delete()
        self.assertEqual(self.client_obj.pending_amount, D("5000"))
        self.assertEqual(ClientLedgerEntry.objects.filter(pk=e.pk).count(), 1)
        e.refresh_from_db()
        self.assertTrue(e.is_deleted)
        self.assertEqual(e.edit_history[-1]["action"], "deleted")

    def test_edit_appends_history_and_flags_is_edited(self):
        e = ClientLedgerEntry.objects.create(
            client=self.client_obj, entry_type="PAYMENT", amount=D("-100")
        )
        e.note = "wrong amount corrected"
        e.amount = D("-150")
        e.save()
        e.refresh_from_db()
        self.assertTrue(e.is_edited)
        self.assertEqual(len(e.edit_history), 1)
        self.assertEqual(e.edit_history[0]["before"]["amount"], "-100.00")
        self.assertEqual(e.edit_history[0]["after"]["amount"], "-150.00")
        self.assertEqual(self.client_obj.pending_amount, D("4850"))


class LabourRollupTests(TestCase):
    """Spec 3.10: employee payments roll up into Labour MonthlyOverhead."""

    def test_employee_payments_sum_into_labour_overhead(self):
        e = Employee.objects.create(name="Ramesh", role="Helper")
        EmployeePayment.objects.create(
            employee=e, month="2026-09", amount_paid=D("4000")
        )
        EmployeePayment.objects.create(
            employee=e, month="2026-09", amount_paid=D("3500")
        )
        labour = OverheadCategory.objects.get(name="Labour")
        row = MonthlyOverhead.objects.get(category=labour, month="2026-09")
        self.assertEqual(row.amount, D("7500"))

    def test_labour_cannot_be_overridden(self):
        """A direct edit of the Labour row is replaced by the payment roll-up."""
        from .views import MonthlyOverheadViewSet

        e = Employee.objects.create(name="Suresh")
        EmployeePayment.objects.create(
            employee=e, month="2026-09", amount_paid=D("1000")
        )
        labour = OverheadCategory.objects.get(name="Labour")
        row = MonthlyOverhead.objects.get(category=labour, month="2026-09")
        row.amount = D("9999")
        row.save()
        EmployeePayment.objects.create(
            employee=e, month="2026-09", amount_paid=D("500")
        )
        # Any sync (payment save or API edit path) restores the exact sum.
        MonthlyOverheadViewSet()._enforce_labour_rollup(row)
        row.refresh_from_db()
        self.assertEqual(row.amount, D("1500"))


class StockAdjustmentTests(TestCase):
    """Spec 4.4: adjustments layered on FIFO, never silent batch edits."""

    def setUp(self):
        self.mat = make_material()
        self.batch = make_batch(self.mat, "100", "10")

    def test_positive_adjustment_adds_zero_price_batch(self):
        adj, unrecorded = apply_stock_adjustment(
            self.mat, D("50"), "Found 50 bottles in godown"
        )
        self.assertEqual(self.mat.stock_in_hand, D("150"))
        self.assertEqual(unrecorded, D("0"))
        new_batch = self.mat.batches.exclude(pk=self.batch.pk).get()
        self.assertEqual(new_batch.price_per_unit, D("0"))
        self.assertEqual(new_batch.quantity_received, D("50"))
        self.assertEqual(adj.reference_batches[0]["batch_id"], new_batch.id)

    def test_negative_adjustment_consumes_oldest_first(self):
        apply_stock_adjustment(self.mat, D("-60"), "Damaged bottles thrown")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_remaining, D("40"))
        self.assertEqual(self.mat.stock_in_hand, D("40"))

    def test_reason_is_mandatory(self):
        with self.assertRaises(ValueError):
            apply_stock_adjustment(self.mat, D("10"), "   ")

    def test_adjustments_do_not_edit_existing_batches_silently(self):
        before_history = list(self.batch.edit_history)
        apply_stock_adjustment(self.mat, D("10"), "extra stock")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.edit_history, before_history)
        self.assertFalse(self.batch.is_edited)


class VendorTests(TestCase):
    """
    Vendor master: contact number + amount owed (purchases received minus
    payments made to the vendor).
    """

    def setUp(self):
        self.vendor = Vendor.objects.create(
            name="Bottle filler", contact_number="9876543210"
        )
        self.material = make_material("Case of bottles", "100", vendor=self.vendor)
        make_batch(self.material, "10", "100")  # 10 cases @ Rs100 = Rs1000

    def test_amount_owed_is_purchases_minus_payments(self):
        self.assertEqual(self.vendor.total_purchased, D("1000"))
        self.assertEqual(self.vendor.amount_owed, D("1000"))
        VendorPayment.objects.create(vendor=self.vendor, amount=D("400"))
        self.assertEqual(self.vendor.total_paid, D("400"))
        self.assertEqual(self.vendor.amount_owed, D("600"))

    def test_soft_deleted_payment_restores_owed_amount(self):
        p = VendorPayment.objects.create(vendor=self.vendor, amount=D("250"))
        self.assertEqual(self.vendor.amount_owed, D("750"))
        p.is_deleted = True
        p.save()
        self.assertEqual(self.vendor.amount_owed, D("1000"))

    def test_only_tagged_materials_count_towards_purchase_total(self):
        other = make_material("Other vendor case", "50")  # no vendor
        make_batch(other, "4", "50")
        self.assertEqual(self.vendor.total_purchased, D("1000"))

    def test_api_lists_vendor_with_contact_and_owed(self):
        api = APIClient()
        resp = api.get("/api/vendors/")
        self.assertEqual(resp.status_code, 200)
        row = resp.data["results"][0]
        self.assertEqual(row["name"], "Bottle filler")
        self.assertEqual(row["contact_number"], "9876543210")
        self.assertEqual(row["amount_owed"], "1000.00")

        # Record a payment through the vendor endpoint.
        resp = api.post(
            f"/api/vendors/{self.vendor.id}/payments/",
            {"amount": "600", "note": "NEFT"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["amount_owed"], "400.00")

    def test_material_api_exposes_vendor(self):
        api = APIClient()
        resp = api.get(f"/api/materials/{self.material.id}/")
        self.assertEqual(resp.data["vendor"], self.vendor.id)
        self.assertEqual(resp.data["vendor_name"], "Bottle filler")


class APITests(TestCase):
    """End-to-end API flows for the Add-Delivery + payment screens."""

    def setUp(self):
        self.api = APIClient()
        self.client_obj = Client.objects.create(name="API Client")
        self.sku = SKU.objects.create(
            description="500ml API", qty_per_case=D("24"), volume_ml=500
        )
        self.bottle = make_material("Bottle API", "240")
        SKUMaterialRequirement.objects.create(
            sku=self.sku, material=self.bottle, qty_per_case=D("1")
        )
        make_batch(self.bottle, "10", "240")
        ClientSKUPrice.objects.create(
            client=self.client_obj, sku=self.sku, selling_price_per_case=D("300")
        )

    def test_delivery_returns_409_then_succeeds_with_force(self):
        payload = {
            "client": self.client_obj.id,
            "sku": self.sku.id,
            "qty_cases": "20",
            "date": "2026-09-10",
        }
        resp = self.api.post("/api/deliveries/", payload, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("shortfall", resp.data)
        self.assertEqual(resp.data["shortfall"][0]["material"], "Bottle API")
        self.assertEqual(StockDelivery.objects.count(), 0)

        resp = self.api.post(
            "/api/deliveries/", {**payload, "force": True}, format="json"
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["stock_shortfall_flag"])
        self.assertIn("cogs", resp.data)
        self.assertEqual(resp.data["total_amount"], "6000.00")

    def test_delivery_auto_fills_price_and_snapshot(self):
        resp = self.api.post(
            "/api/deliveries/",
            {
                "client": self.client_obj.id,
                "sku": self.sku.id,
                "qty_cases": "2",
                "date": "2026-09-10",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        delivery = StockDelivery.objects.get()
        self.assertEqual(delivery.selling_price_per_case, D("300"))
        # COGS frozen server-side and non-zero (raw materials at minimum).
        self.assertGreater(delivery.cogs_per_case_snapshot, D("0"))

    def test_payment_endpoint_decreases_pending(self):
        ClientLedgerEntry.objects.create(
            client=self.client_obj, entry_type="DELIVERY", amount=D("1000")
        )
        resp = self.api.post(
            f"/api/clients/{self.client_obj.id}/payment/",
            {"amount": "400", "note": "Cash"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["pending_amount"], "600.00")
        entry = ClientLedgerEntry.objects.get(pk=resp.data["entry"]["id"])
        self.assertEqual(entry.amount, D("-400"))  # stored negative

    def test_ledger_returns_running_balances(self):
        ClientLedgerEntry.objects.create(
            client=self.client_obj,
            entry_type="DELIVERY",
            amount=D("1000"),
            date=date(2026, 9, 1),
        )
        ClientLedgerEntry.objects.create(
            client=self.client_obj,
            entry_type="PAYMENT",
            amount=D("-400"),
            date=date(2026, 9, 5),
        )
        resp = self.api.get(f"/api/clients/{self.client_obj.id}/ledger/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["pending_amount"], "600.00")
        entries = resp.data["entries"]
        # Newest first, but balances computed chronologically.
        self.assertEqual(entries[0]["running_balance"], "600.00")
        self.assertEqual(entries[1]["running_balance"], "1000.00")

    def test_dashboard_endpoint(self):
        self.api.post(
            "/api/deliveries/",
            {
                "client": self.client_obj.id,
                "sku": self.sku.id,
                "qty_cases": "2",
                "date": "2026-09-10",
            },
            format="json",
        )
        resp = self.api.get("/api/dashboard/?month=2026-09")
        self.assertEqual(resp.status_code, 200)
        stats = resp.data["stats"]
        self.assertEqual(stats["total_cases"], "2")
        self.assertEqual(stats["cases_sold_per_sku"][0]["cases"], "2")
        stock_rows = {s["name"]: s for s in resp.data["stock"]}
        # 10 cases in stock - 2 cases delivered = 8.
        self.assertEqual(stock_rows["Bottle API"]["stock_in_hand"], "8")

    def test_report_endpoint(self):
        self.api.post(
            "/api/deliveries/",
            {
                "client": self.client_obj.id,
                "sku": self.sku.id,
                "qty_cases": "2",
                "date": "2026-09-10",
            },
            format="json",
        )
        resp = self.api.get(f"/api/reports/?sku={self.sku.id}&granularity=day")
        self.assertEqual(resp.status_code, 200)
        # Rows are sorted by bucket; batch arrivals may add earlier buckets.
        row = next(r for r in resp.data["rows"] if r["bucket"] == "2026-09-10")
        self.assertEqual(row["sold_cases"], "2")
        self.assertIn("rolling_30d_avg_cases_per_day", resp.data)





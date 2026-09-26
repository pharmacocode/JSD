"""
Unit tests for the FIFO costing engine + COGS behavior (spec Sections 5-6),
audit rules (3.7), ledger integrity (3.2), and API flows (4.2).
"""

from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from .cogs import (
    DeliveryShortfall,
    apply_stock_adjustment,
    check_shortfall,
    check_shortfall_many,
    consume_fifo,
    create_deliveries,
    create_delivery,
    stock_available,
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
    """
    Spec 5/6: the COGS snapshot is WRITTEN at delivery creation for the audit
    trail (3.7) — while every displayed cost stays dynamic (user request:
    no frozen costs anywhere; charts and lists recompute from current prices).
    """

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

    def test_snapshot_is_audit_only_and_displayed_costs_stay_dynamic(self):
        """
        The snapshot columns are written once and never rewritten (audit), but
        every figure the UI shows is recomputed from CURRENT data — a later
        price edit moves the direct cost while the snapshot itself does not.
        """
        delivery, _result = create_delivery(
            self.client_obj, self.sku, D("10"), delivery_date=date(2026, 9, 10)
        )
        frozen_base = delivery.base_cogs_per_case_snapshot
        frozen_full = delivery.cogs_per_case_snapshot

        # Prices change AFTER the delivery: the bottle batch is repriced,
        # the master display price and the month's overhead move too.
        MaterialBatch.objects.filter(material=self.bottle).update(
            price_per_unit=D("40")
        )
        rent = OverheadCategory.objects.get(name="Rent")
        MonthlyOverhead.objects.filter(category=rent).update(amount=D("99999"))
        Material.objects.filter(pk=self.bottle.pk).update(
            current_price_per_unit=D("1")
        )

        delivery.refresh_from_db()
        # The audit columns keep the creation-time figures …
        self.assertEqual(delivery.base_cogs_per_case_snapshot, frozen_base)
        self.assertEqual(delivery.cogs_per_case_snapshot, frozen_full)
        # … while every displayed value follows the current data: the bottle
        # batch now costs 40/case (queue repriced), the label still 12, and
        # print comes from the current config as before.
        print_per_case = (D("50") + D("10")) / D("100") * D("24") * D("1.05")
        expected_base = money(D("40") + D("12") + print_per_case)
        self.assertEqual(delivery.base_cogs_per_case_current, expected_base)
        # Overhead stays live for the month: 99999 / 10 cases.
        self.assertEqual(
            delivery.overhead_per_case_current, money(D("99999") / D("10"))
        )
        self.assertEqual(
            delivery.cogs_per_case_current,
            money(expected_base + delivery.overhead_per_case_current),
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


class NegativeStockMarkingTests(TestCase):
    """
    User request: a delivery marked when there is NO stock in hand must drive
    stock in hand NEGATIVE (never silently stay at 0) so the user reconciles
    via a Stock Adjustment or a retrospective (backdated) arrival.
    """

    def setUp(self):
        self.client_obj = Client.objects.create(name="Neg Stock Co")
        self.sku = SKU.objects.create(
            description="500ml Neg", qty_per_case=D("24"), volume_ml=500
        )
        self.bottle = make_material("Bottle Neg", "240")
        SKUMaterialRequirement.objects.create(
            sku=self.sku, material=self.bottle, qty_per_case=D("1")
        )
        ClientSKUPrice.objects.create(
            client=self.client_obj, sku=self.sku, selling_price_per_case=D("300")
        )

    def test_forced_delivery_without_batches_marks_stock_negative(self):
        """Zero batches at all -> a deficit carrier batch appears at -10."""
        delivery, result = create_delivery(
            self.client_obj,
            self.sku,
            D("10"),
            delivery_date=date(2026, 9, 5),
            force=True,
        )
        self.assertTrue(delivery.stock_shortfall_flag)
        self.assertEqual(self.bottle.stock_in_hand, D("-10"))
        carrier = self.bottle.batches.get()
        self.assertEqual(carrier.quantity_received, D("0"))
        self.assertEqual(carrier.quantity_remaining, D("-10"))
        self.assertEqual(carrier.arrival_date, date(2026, 9, 5))
        # The deficit is BOOKED (not "unrecorded") and costed at the master
        # display price — the same figure the Add-Delivery preview quotes for
        # an empty queue: 10 cases x 240 / 10 cases, no print config.
        self.assertFalse(result["has_unrecorded_shortfall"])
        self.assertEqual(delivery.base_cogs_per_case_snapshot, money(D("240")))
        entry = result["consumption"][0]["consumed"][0]
        self.assertEqual(entry["batch_id"], carrier.id)
        self.assertTrue(entry["deficit"])

    def test_further_deliveries_deepen_the_negative(self):
        create_delivery(
            self.client_obj,
            self.sku,
            D("10"),
            delivery_date=date(2026, 9, 5),
            force=True,
        )  # -> -10 on the carrier
        create_delivery(
            self.client_obj,
            self.sku,
            D("5"),
            delivery_date=date(2026, 9, 6),
            force=True,
        )  # -> -15, same single row
        self.assertEqual(self.bottle.stock_in_hand, D("-15"))
        carrier = self.bottle.batches.get()
        self.assertEqual(carrier.quantity_remaining, D("-15"))

    def test_exhausted_batch_goes_negative_on_the_next_delivery(self):
        make_batch(self.bottle, "10", "240")
        first, _ = create_delivery(
            self.client_obj, self.sku, D("10"), delivery_date=date(2026, 9, 1)
        )
        self.assertFalse(first.stock_shortfall_flag)  # exact stock, not short
        self.assertEqual(self.bottle.stock_in_hand, D("0"))
        second, _ = create_delivery(
            self.client_obj,
            self.sku,
            D("4"),
            delivery_date=date(2026, 9, 2),
            force=True,
        )
        self.assertTrue(second.stock_shortfall_flag)
        self.assertEqual(self.bottle.stock_in_hand, D("-4"))
        self.assertEqual(self.bottle.batches.get().quantity_remaining, D("-4"))

    def test_reconciliation_paths_clear_the_negative(self):
        create_delivery(
            self.client_obj,
            self.sku,
            D("10"),
            delivery_date=date(2026, 9, 5),
            force=True,
        )  # -> -10
        # (a) Stock Adjustment — found stock.
        apply_stock_adjustment(self.bottle, D("6"), "Found 6 cases in godown")
        self.assertEqual(self.bottle.stock_in_hand, D("-4"))
        # (b) Retrospective (backdated) arrival — the missing cases turn up
        # dated before the delivery; stock nets back positive.
        make_batch(self.bottle, "20", "235", arrival=date(2026, 9, 4))
        self.assertEqual(self.bottle.stock_in_hand, D("16"))


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

    def test_forced_delivery_surfaces_negative_stock(self):
        """Zero stock in hand -> the API shows negative stock, not 0."""
        MaterialBatch.objects.filter(material=self.bottle).delete()
        resp = self.api.post(
            "/api/deliveries/",
            {
                "client": self.client_obj.id,
                "sku": self.sku.id,
                "qty_cases": "10",
                "date": "2026-09-10",
                "force": True,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["stock_shortfall_flag"])
        batches = self.api.get(f"/api/materials/{self.bottle.id}/batches/")
        self.assertEqual(D(batches.data["stock_in_hand"]), D("-10"))
        dash = self.api.get("/api/dashboard/?month=2026-09").data
        row = next(r for r in dash["stock"] if r["name"] == "Bottle API")
        self.assertEqual(row["stock_in_hand"], "-10")
        self.assertTrue(row["below_alert"])
        # The deficit carrier is a booking, not an arrival — it must not
        # appear among the month's inward line items.
        self.assertNotIn(
            "Bottle API",
            [r["material"] for r in dash["stats"]["inward_materials"]],
        )

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
        # COGS recorded server-side and non-zero (raw materials at minimum).
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

    def test_report_all_skus_aggregates_every_sku(self):
        """sku=all buckets sold quantities across every SKU (user request)."""
        sku2 = SKU.objects.create(
            description="1L API", qty_per_case=D("12"), volume_ml=1000
        )
        SKUMaterialRequirement.objects.create(
            sku=sku2, material=self.bottle, qty_per_case=D("1")
        )
        ClientSKUPrice.objects.create(
            client=self.client_obj, sku=sku2, selling_price_per_case=D("500")
        )
        for sku_id, qty, day in (
            (self.sku.id, "2", "2026-09-10"),
            (sku2.id, "3", "2026-09-12"),
        ):
            resp = self.api.post(
                "/api/deliveries/",
                {
                    "client": self.client_obj.id,
                    "sku": sku_id,
                    "qty_cases": qty,
                    "date": day,
                },
                format="json",
            )
            self.assertEqual(resp.status_code, 201)

        resp = self.api.get("/api/reports/?sku=all&granularity=day")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["sku"]["description"], "All SKUs")
        sold = {r["bucket"]: D(r["sold_cases"]) for r in resp.data["rows"]}
        self.assertEqual(sold.get("2026-09-10"), D("2"))
        self.assertEqual(sold.get("2026-09-12"), D("3"))
        # 'all' is the only extra value — a missing/unknown sku still 400s.
        self.assertEqual(self.api.get("/api/reports/").status_code, 400)
        self.assertEqual(
            self.api.get("/api/reports/?sku=999999").status_code, 400
        )
        # The single-SKU view still works and shows only its own sales.
        resp = self.api.get(f"/api/reports/?sku={self.sku.id}&granularity=day")
        sold = {r["bucket"]: D(r["sold_cases"]) for r in resp.data["rows"]}
        self.assertEqual(sold.get("2026-09-10"), D("2"))
        self.assertNotIn("2026-09-12", sold)

    def test_client_lifetime_revenue_and_profit_toggle(self):
        """Lifetime stats: revenue, direct cost, overhead share, both profits."""
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
        rent = OverheadCategory.objects.create(name="Rent Lifetime")
        MonthlyOverhead.objects.create(
            category=rent, month="2026-09", amount=D("60")
        )

        resp = self.api.get(f"/api/clients/{self.client_obj.id}/lifetime/")
        self.assertEqual(resp.status_code, 200)
        data = resp.data
        self.assertEqual(data["delivery_count"], 1)
        self.assertEqual(D(data["cases"]), D("2"))
        # 2 cases x Rs300 selling price.
        self.assertEqual(D(data["revenue"]), D("600.00"))
        # Direct: 2 cases x 1 bottle x Rs240 FIFO (no print config).
        self.assertEqual(D(data["direct_cost"]), D("480.00"))
        # Overhead: Rs60 for the month / 2 cases sold = Rs30/case x 2 cases,
        # i.e. the client carries its own share of the month's overhead.
        self.assertEqual(D(data["overhead"]), D("60.00"))
        self.assertEqual(D(data["profit_without_overhead"]), D("120.00"))
        self.assertEqual(D(data["profit_with_overhead"]), D("60.00"))
        # Soft-deleted deliveries never count towards lifetime figures.
        StockDelivery.objects.filter(client=self.client_obj).update(
            is_deleted=True
        )
        resp = self.api.get(f"/api/clients/{self.client_obj.id}/lifetime/")
        self.assertEqual(D(resp.data["revenue"]), D("0.00"))
        self.assertEqual(D(resp.data["profit_with_overhead"]), D("0.00"))


class ClientPendingMarkerTests(TestCase):
    """
    User request: the client's pending amount is editable at any time, together
    with the date it refers to. Ledger entries dated strictly after the marked
    day are added on top; everything on or before it is already inside the
    entered figure (superseded, never counted twice).
    """

    def setUp(self):
        self.api = APIClient()
        self.client_obj = Client.objects.create(name="Marker Client")
        self.older = ClientLedgerEntry.objects.create(
            client=self.client_obj,
            entry_type="DELIVERY",
            amount=D("1000"),
            date=date(2026, 9, 10),
        )
        self.later = ClientLedgerEntry.objects.create(
            client=self.client_obj,
            entry_type="DELIVERY",
            amount=D("500"),
            date=date(2026, 9, 30),
        )
        self.url = f"/api/clients/{self.client_obj.id}/pending/"

    def test_preview_splits_entries_without_saving(self):
        # 2,500 marked as of 25 Sep -> only the 30 Sep delivery is added on top.
        resp = self.api.get(self.url, {"as_of": "2026-09-25", "amount": "2500"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["amount"], "2500.00")
        self.assertEqual(resp.data["after"]["count"], 1)
        self.assertEqual(resp.data["after"]["total"], "500.00")
        self.assertEqual(resp.data["before"]["count"], 1)
        self.assertEqual(resp.data["before"]["total"], "1000.00")
        self.assertEqual(resp.data["total"], "3000.00")
        self.assertEqual(resp.data["pure_total"], "1500.00")
        self.client_obj.refresh_from_db()
        self.assertIsNone(self.client_obj.pending_as_of_date)  # preview only

    def test_marker_is_settable_editable_and_clearable(self):
        resp = self.api.post(
            self.url, {"amount": "2500", "date": "2026-09-25"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["pending_amount"], "3000.00")
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.pending_as_of_date, date(2026, 9, 25))
        self.assertEqual(self.client_obj.pending_amount, D("3000"))

        # Editable at any time: same endpoint, new figure + new date.
        resp = self.api.post(
            self.url, {"amount": "1200", "date": "2026-09-09"}, format="json"
        )
        self.assertEqual(resp.data["amount"], "1200.00")
        self.assertEqual(resp.data["after"]["count"], 2)  # both are later now
        self.assertEqual(resp.data["pending_amount"], "2700.00")

        # Clearing returns to the plain live ledger sum.
        resp = self.api.delete(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["marker_active"])
        self.assertEqual(resp.data["pending_amount"], "1500.00")
        self.client_obj.refresh_from_db()
        self.assertIsNone(self.client_obj.pending_as_of_date)
        self.assertEqual(self.client_obj.pending_amount, D("1500"))

    def test_entry_on_the_marked_day_is_inside_the_figure(self):
        ClientLedgerEntry.objects.create(
            client=self.client_obj,
            entry_type="PAYMENT",
            amount=D("-200"),
            date=date(2026, 9, 25),
        )
        self.client_obj.pending_as_of_amount = D("2500")
        self.client_obj.pending_as_of_date = date(2026, 9, 25)
        self.client_obj.save()
        # 10 Sep delivery + the 25 Sep payment sit inside the 2,500 figure;
        # only the 30 Sep delivery is added -> 3,000.
        self.assertEqual(self.client_obj.pending_amount, D("3000"))

    def test_amount_and_date_are_required(self):
        resp = self.api.post(self.url, {"date": "2026-09-25"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["detail"], "amount is required")
        resp = self.api.post(self.url, {"amount": "100"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("date", resp.data["detail"])

    def test_ledger_flags_superseded_entries_and_restarts_balance(self):
        self.client_obj.pending_as_of_amount = D("2500")
        self.client_obj.pending_as_of_date = date(2026, 9, 25)
        self.client_obj.save()
        resp = self.api.get(f"/api/clients/{self.client_obj.id}/ledger/")
        rows = {r["id"]: r for r in resp.data["entries"]}
        self.assertTrue(rows[self.older.id]["is_superseded"])
        self.assertIsNone(rows[self.older.id]["running_balance"])
        self.assertFalse(rows[self.later.id]["is_superseded"])
        self.assertEqual(rows[self.later.id]["running_balance"], "3000.00")
        self.assertEqual(resp.data["superseded_count"], 1)
        self.assertEqual(resp.data["superseded_total"], "1000.00")
        self.assertEqual(resp.data["pending_as_of_date"], "2026-09-25")

    def test_client_payload_exposes_marker_fields(self):
        resp = self.api.get(f"/api/clients/{self.client_obj.id}/")
        self.assertEqual(resp.data["pending_as_of_amount"], "0.00")
        self.assertIsNone(resp.data["pending_as_of_date"])
        # A plain PATCH writes the marker too (fields are editable).
        resp = self.api.patch(
            f"/api/clients/{self.client_obj.id}/",
            {"pending_as_of_amount": "2500.00", "pending_as_of_date": "2026-09-25"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["pending_amount"], "3000.00")


class VendorPayableMarkerTests(TestCase):
    """
    User request (vendor side): what we owe a vendor is editable at any time,
    together with the date it refers to. Arrived stock and payments dated
    strictly after the marked day are added on top of the entered figure.
    """

    def setUp(self):
        self.api = APIClient()
        self.vendor = Vendor.objects.create(name="Marker Vendor")
        self.material = make_material("Cap for marker", "5", vendor=self.vendor)
        make_batch(self.material, "100", "5", date(2026, 9, 10))  # 500
        make_batch(self.material, "200", "5", date(2026, 9, 30))  # 1000
        VendorPayment.objects.create(
            vendor=self.vendor, amount=D("300"), date=date(2026, 9, 20)
        )
        VendorPayment.objects.create(
            vendor=self.vendor, amount=D("100"), date=date(2026, 10, 5)
        )
        self.url = f"/api/vendors/{self.vendor.id}/payable/"

    def test_without_marker_payable_is_purchases_minus_payments(self):
        self.assertEqual(self.vendor.amount_owed, D("1100"))  # 1500 − 400

    def test_marker_adds_only_later_movements(self):
        resp = self.api.post(
            self.url, {"amount": "250", "date": "2026-09-25"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["amount"], "250.00")
        # After 25 Sep: 1,000 purchase (30 Sep) − 100 payment (5 Oct).
        self.assertEqual(resp.data["after"]["count"], 2)
        self.assertEqual(resp.data["after"]["total"], "900.00")
        self.assertEqual(resp.data["before"]["count"], 2)  # 10 Sep + 20 Sep
        self.assertEqual(resp.data["total"], "1150.00")
        self.assertEqual(resp.data["amount_owed"], "1150.00")
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.payable_as_of_date, date(2026, 9, 25))
        self.assertEqual(self.vendor.amount_owed, D("1150"))
        # Lifetime tiles keep showing all-time totals.
        self.assertEqual(self.vendor.total_purchased, D("1500"))
        self.assertEqual(self.vendor.total_paid, D("400"))

    def test_movement_on_the_marked_day_is_inside_the_figure(self):
        VendorPayment.objects.create(
            vendor=self.vendor, amount=D("300"), date=date(2026, 9, 25)
        )
        self.vendor.payable_as_of_amount = D("250")
        self.vendor.payable_as_of_date = date(2026, 9, 25)
        self.vendor.save()
        self.assertEqual(self.vendor.amount_owed, D("1150"))

    def test_soft_deleted_movements_are_ignored(self):
        batch = make_batch(self.material, "50", "5", date(2026, 10, 10))
        batch.soft_delete()
        payment = VendorPayment.objects.create(
            vendor=self.vendor, amount=D("50"), date=date(2026, 10, 10)
        )
        payment.is_deleted = True
        payment.save()
        self.vendor.payable_as_of_amount = D("250")
        self.vendor.payable_as_of_date = date(2026, 9, 25)
        self.vendor.save()
        self.assertEqual(self.vendor.amount_owed, D("1150"))

    def test_clear_marker_restores_purchases_minus_payments(self):
        self.vendor.payable_as_of_amount = D("250")
        self.vendor.payable_as_of_date = date(2026, 9, 25)
        self.vendor.save()
        resp = self.api.delete(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["marker_active"])
        self.assertEqual(resp.data["amount_owed"], "1100.00")
        self.vendor.refresh_from_db()
        self.assertIsNone(self.vendor.payable_as_of_date)

    def test_vendor_payload_exposes_marker_fields(self):
        resp = self.api.get(f"/api/vendors/{self.vendor.id}/")
        self.assertEqual(resp.data["payable_as_of_amount"], "0.00")
        self.assertIsNone(resp.data["payable_as_of_date"])
        self.assertEqual(resp.data["amount_owed"], "1100.00")


class EmployeeEditTests(TestCase):
    """
    User request: employee details were create-only from the UI — name, role,
    monthly pay and active flag must be editable at any time.
    """

    def setUp(self):
        self.api = APIClient()
        self.employee = Employee.objects.create(
            name="Ramesh", role="Helper", monthly_pay=D("12000")
        )
        self.url = f"/api/employees/{self.employee.id}/"

    def test_employee_details_can_be_edited(self):
        resp = self.api.put(
            self.url,
            {
                "name": "Ramesh Kumar",
                "role": "Supervisor",
                "monthly_pay": "15000",
                "active": True,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.name, "Ramesh Kumar")
        self.assertEqual(self.employee.role, "Supervisor")
        self.assertEqual(self.employee.monthly_pay, D("15000"))

    def test_single_field_edits_and_deactivation(self):
        resp = self.api.patch(self.url, {"monthly_pay": "13000"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.monthly_pay, D("13000"))
        self.assertEqual(self.employee.name, "Ramesh")  # untouched

        resp = self.api.patch(self.url, {"active": False}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.active)
        # Retired staff stay listed — their payments keep rolling into Labour.
        listing = self.api.get("/api/employees/")
        rows = listing.data["results"] if "results" in listing.data else listing.data
        self.assertEqual([e["name"] for e in rows], ["Ramesh"])

    def test_editing_an_employee_does_not_disturb_the_labour_rollup(self):
        EmployeePayment.objects.create(
            employee=self.employee, month="2026-09", amount_paid=D("5000")
        )
        self.api.patch(self.url, {"monthly_pay": "13000"}, format="json")
        labour = MonthlyOverhead.objects.get(month="2026-09", category__name="Labour")
        self.assertEqual(labour.amount, D("5000"))


class InwardLineItemEditTests(TestCase):
    """
    User request: the inward-material line items on the Home screen are
    editable; correcting one moves every derived figure with it — stock in
    hand, the FIFO queue, the vendor payable and the dashboard rows.
    """

    def setUp(self):
        self.api = APIClient()
        self.vendor = Vendor.objects.create(name="Arrival Vendor")
        self.mat = make_material(
            "Bottle API", "10", vendor=self.vendor, stock_alert_qty=D("20")
        )
        self.batch = make_batch(self.mat, "100", "10", date(2026, 9, 5))
        self.url = f"/api/batches/{self.batch.id}/"

    def test_increasing_received_raises_stock_and_payable(self):
        resp = self.api.patch(
            self.url, {"quantity_received": "150"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_received, D("150"))
        self.assertEqual(self.batch.quantity_remaining, D("150"))
        self.assertEqual(self.mat.stock_in_hand, D("150"))
        self.assertEqual(self.vendor.amount_owed, D("1500"))  # 150 × 10
        self.assertTrue(self.batch.is_edited)  # the edit is audited
        # DRF decimal fields render with the field's 4-dp precision.
        self.assertEqual(resp.data["consumed"], "0.0000")
        self.assertEqual(resp.data["material_unit"], "cases")

    def test_reducing_keeps_what_was_already_consumed(self):
        consume_fifo(self.mat, D("40"))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_remaining, D("60"))
        resp = self.api.patch(
            self.url, {"quantity_received": "80", "price_per_unit": "11"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.batch.refresh_from_db()
        # The 40 consumed cases stay consumed: 80 − 40 = 40 in hand.
        self.assertEqual(self.batch.quantity_remaining, D("40"))
        self.assertEqual(self.batch.consumed_quantity, D("40"))
        self.assertEqual(self.mat.stock_in_hand, D("40"))
        self.assertEqual(self.vendor.amount_owed, D("880"))  # 80 × 11
        self.assertFalse(self.mat.is_below_alert)  # 40 >= alert 20

    def test_reducing_below_consumed_goes_visible_negative(self):
        consume_fifo(self.mat, D("90"))
        resp = self.api.patch(self.url, {"quantity_received": "50"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_remaining, D("-40"))
        self.assertEqual(self.mat.stock_in_hand, D("-40"))
        self.assertTrue(self.mat.is_below_alert)  # reconciliable via adjustment

    def test_corrected_price_drives_fifo_cost_from_then_on(self):
        self.api.patch(self.url, {"price_per_unit": "12"}, format="json")
        _consumption, cost, _short = consume_fifo(self.mat, D("10"))
        self.assertEqual(cost, D("120"))  # new landed price used by FIFO

    def test_soft_deleted_arrival_leaves_stock_and_payable(self):
        resp = self.api.delete(self.url)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self.mat.stock_in_hand, D("0"))
        self.assertEqual(self.vendor.amount_owed, D("0"))
        batches = self.api.get(f"/api/materials/{self.mat.id}/batches/")
        self.assertEqual(batches.data["batches"], [])


class DashboardSummaryTests(TestCase):
    """
    User request: the Home summary shows profit too, one chart area is driven by
    the selected metric (cases per SKU / revenue per client / profit per client,
    with an overhead toggle), and each inward row carries its editable line
    items. These tests pin the figures behind those charts.
    """

    def setUp(self):
        self.api = APIClient()
        self.alpha = Client.objects.create(name="Alpha")
        self.beta = Client.objects.create(name="Beta")
        self.mat = make_material("Bottle API", "10")
        make_batch(self.mat, "100", "10", date(2026, 9, 1))
        self.sku = SKU.objects.create(description="Serum 500ml", qty_per_case=D("1"))
        SKUMaterialRequirement.objects.create(
            sku=self.sku, material=self.mat, qty_per_case=D("1")
        )
        self.api.post(
            "/api/deliveries/",
            {
                "client": self.alpha.id,
                "sku": self.sku.id,
                "qty_cases": "2",
                "selling_price_per_case": "500",
                "date": "2026-09-10",
            },
            format="json",
        )
        self.api.post(
            "/api/deliveries/",
            {
                "client": self.beta.id,
                "sku": self.sku.id,
                "qty_cases": "5",
                "selling_price_per_case": "400",
                "date": "2026-09-12",
            },
            format="json",
        )
        MonthlyOverhead.objects.create(
            category=OverheadCategory.objects.get(name="Rent"),
            month="2026-09",
            amount=D("350"),
        )

    def stats(self):
        return self.api.get("/api/dashboard/?month=2026-09").data["stats"]

    def test_totals_include_profit_with_and_without_overhead(self):
        stats = self.stats()
        self.assertEqual(stats["total_cases"], "7")
        self.assertEqual(D(stats["total_revenue"]), D("3000"))  # 2×500 + 5×400
        self.assertEqual(D(stats["total_direct_cost"]), D("70"))  # 7 × 10 FIFO
        self.assertEqual(D(stats["total_overhead"]), D("350"))
        self.assertEqual(D(stats["total_profit_excl_overhead"]), D("2930"))
        self.assertEqual(D(stats["total_profit_incl_overhead"]), D("2580"))
        # 350 overhead across 7 cases = 50/case.
        self.assertEqual(D(stats["overhead_per_case"]), D("50.00"))

    def test_client_breakdown_is_high_to_low_and_adds_up(self):
        stats = self.stats()
        rows = stats["client_breakdown"]
        self.assertEqual([r["client"] for r in rows], ["Beta", "Alpha"])
        self.assertEqual(D(rows[0]["revenue"]), D("2000"))
        self.assertEqual(D(rows[1]["revenue"]), D("1000"))
        self.assertEqual(D(rows[0]["cases"]), D("5"))
        self.assertEqual(D(rows[0]["overhead"]), D("250"))  # 5 × 50
        # The bars add up to the summary cards (cards drive the charts).
        self.assertEqual(
            sum((D(r["profit_incl_overhead"]) for r in rows), D("0")),
            D(stats["total_profit_incl_overhead"]),
        )
        self.assertEqual(
            sum((D(r["profit_excl_overhead"]) for r in rows), D("0")),
            D(stats["total_profit_excl_overhead"]),
        )

    def test_later_overhead_edits_move_profit_live(self):
        MonthlyOverhead.objects.filter(
            month="2026-09", category__name="Rent"
        ).update(amount=D("1050"))
        stats = self.stats()
        self.assertEqual(D(stats["total_overhead"]), D("1050"))
        self.assertEqual(D(stats["total_profit_incl_overhead"]), D("1880"))
        self.assertEqual(D(stats["total_profit_excl_overhead"]), D("2930"))
        self.assertEqual(D(stats["overhead_per_case"]), D("150.00"))

    def test_cases_sold_per_sku_feeds_the_default_chart(self):
        rows = self.stats()["cases_sold_per_sku"]
        self.assertEqual(rows[0]["sku"], "Serum 500ml")
        self.assertEqual(rows[0]["cases"], "7")
        self.assertEqual(D(rows[0]["revenue"]), D("3000"))

    def test_sku_client_matrix_drilldown_sums_match_the_client_bars(self):
        """
        Chart drill-downs (user request): one matrix row per client x SKU for
        the month, and those rows add back up to the client_breakdown bars
        exactly — revenue, cases, overhead share and both profit figures.
        """
        # A second SKU delivered to Alpha in the same month (3 more cases).
        sku2 = SKU.objects.create(description="Serum 1L", qty_per_case=D("1"))
        SKUMaterialRequirement.objects.create(
            sku=sku2, material=self.mat, qty_per_case=D("1")
        )
        resp = self.api.post(
            "/api/deliveries/",
            {
                "client": self.alpha.id,
                "sku": sku2.id,
                "qty_cases": "3",
                "selling_price_per_case": "600",
                "date": "2026-09-15",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        stats = self.stats()
        matrix = stats["sku_client_matrix"]
        # 10 cases sold now: overhead per case = 350 / 10 = 35.
        self.assertEqual(D(stats["overhead_per_case"]), D("35.00"))

        alpha_rows = sorted(
            (r for r in matrix if r["client"] == "Alpha"), key=lambda r: r["sku"]
        )
        self.assertEqual(
            [r["sku"] for r in alpha_rows], ["Serum 1L", "Serum 500ml"]
        )
        self.assertEqual([D(r["cases"]) for r in alpha_rows], [D("3"), D("2")])
        self.assertEqual(D(alpha_rows[0]["revenue"]), D("1800"))
        self.assertEqual(D(alpha_rows[1]["revenue"]), D("1000"))

        # Every client's matrix rows sum to its bar (charts must agree).
        bars = {r["client"]: r for r in stats["client_breakdown"]}
        self.assertEqual(set(bars), {"Alpha", "Beta"})
        for name, bar in bars.items():
            rows = [r for r in matrix if r["client"] == name]
            for key in ("cases", "revenue", "overhead"):
                self.assertEqual(
                    sum((D(r[key]) for r in rows), D("0")),
                    D(bar[key]),
                    f"{name} {key} drill rows must add up to the bar",
                )
            self.assertEqual(
                sum((D(r["profit_excl_overhead"]) for r in rows), D("0")),
                D(bar["profit_excl_overhead"]),
            )
            self.assertEqual(
                sum((D(r["profit_incl_overhead"]) for r in rows), D("0")),
                D(bar["profit_incl_overhead"]),
            )

        # The cost chain: revenue - materials - print - overhead = profit.
        for r in matrix:
            self.assertEqual(
                D(r["materials_cost"]) + D(r["print_cost"]), D(r["direct_cost"])
            )
            self.assertEqual(
                D(r["revenue"])
                - D(r["materials_cost"])
                - D(r["print_cost"])
                - D(r["overhead"]),
                D(r["profit_incl_overhead"]),
            )
            self.assertEqual(
                D(r["revenue"]) - D(r["direct_cost"]),
                D(r["profit_excl_overhead"]),
            )

    def test_chart_costs_follow_current_prices_not_frozen_snapshots(self):
        """
        User request — no frozen costs: the chart's raw material and print
        costs recompute from CURRENT data. Editing the batch price or adding
        a print config AFTER the deliveries were made must move the numbers.
        """
        stats = self.stats()
        self.assertEqual(D(stats["total_direct_cost"]), D("70"))  # 7 × ₹10

        # Raw material price edit -> materials cost moves immediately.
        batch = MaterialBatch.objects.filter(material=self.mat).first()
        batch.price_per_unit = D("20")
        batch.save()
        stats = self.stats()
        self.assertEqual(D(stats["total_direct_cost"]), D("140"))  # 7 × ₹20
        self.assertEqual(
            sum((D(r["direct_cost"]) for r in stats["client_breakdown"]), D("0")),
            D(stats["total_direct_cost"]),
        )

        # A print config that did not exist at delivery time flows in too:
        # per case = (100 + 50) / 10 labels × 1 label per case = ₹15.
        SKUPrintCost.objects.create(
            sku=self.sku,
            paper_cost=D("100"),
            print_cost_per_paper=D("50"),
            labels_per_paper=D("10"),
            wastage_percent=D("0"),
        )
        stats = self.stats()
        self.assertEqual(D(stats["total_direct_cost"]), D("245"))  # 7 × (20+15)
        matrix = stats["sku_client_matrix"]
        for r in matrix:
            self.assertEqual(D(r["print_cost"]), D(r["cases"]) * D("15"))
            self.assertEqual(
                D(r["materials_cost"]) + D(r["print_cost"]), D(r["direct_cost"])
            )
        self.assertEqual(
            sum((D(r["direct_cost"]) for r in matrix), D("0")),
            D(stats["total_direct_cost"]),
        )

    def test_delivery_api_costs_are_dynamic(self):
        """
        The deliveries API serves recomputed costs (today's queue + current
        config), not the creation-time snapshot values.
        """
        rows = self.api.get("/api/deliveries/?month=2026-09").data
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(D(row["base_cogs_per_case"]), D("10"))

        MaterialBatch.objects.filter(material=self.mat).update(
            price_per_unit=D("25")
        )
        rows = self.api.get("/api/deliveries/?month=2026-09").data
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        for row in rows:
            self.assertEqual(D(row["base_cogs_per_case"]), D("25"))
            self.assertEqual(
                D(row["cogs_per_case_current"]),
                D("25") + D(row["overhead_per_case_current"]),
            )
            self.assertEqual(
                D(row["total_cogs"]),
                money(D(row["qty_cases"]) * D(row["cogs_per_case_current"])),
            )

    def test_overhead_categories_feed_the_profit_drilldown(self):
        stats = self.stats()
        cats = stats["overhead_categories"]
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0]["category"], "Rent")
        self.assertEqual(D(cats[0]["amount"]), D("350"))
        # The categories total the month overhead the bars already use.
        self.assertEqual(
            sum((D(c["amount"]) for c in cats), D("0")),
            D(stats["total_overhead"]),
        )

    def test_inward_rows_carry_editable_line_items(self):
        stats = self.stats()
        row = stats["inward_materials"][0]
        self.assertEqual(row["material"], "Bottle API")
        self.assertEqual(row["quantity"], "100")
        self.assertEqual(len(row["items"]), 1)
        item = row["items"][0]
        self.assertEqual(item["quantity_received"], "100")
        self.assertEqual(item["quantity_remaining"], "93")  # 7 consumed
        self.assertEqual(item["consumed"], "7")
        self.assertEqual(item["price_per_unit"], "10.00")

        # Correcting the line item recalculates the row and stock in hand.
        self.api.patch(
            f"/api/batches/{item['id']}/", {"quantity_received": "50"}, format="json"
        )
        payload = self.api.get("/api/dashboard/?month=2026-09").data
        self.assertEqual(payload["stats"]["inward_materials"][0]["quantity"], "50")
        stock = {s["name"]: s for s in payload["stock"]}
        self.assertEqual(stock["Bottle API"]["stock_in_hand"], "43")  # 50 − 7


class MultiSKUDeliveryTests(TestCase):
    """
    User request: ONE delivery can carry several SKUs, each with its own
    quantity (and price).

    Stored as one StockDelivery row per line, so FIFO consumption, the
    creation-time COGS snapshot (audit only) and the generated ledger entry
    stay exactly as before — but the stock check covers the delivery as a
    whole (shared materials summed) and the shared FIFO queue is walked
    oldest-first ACROSS the lines.
    """

    def setUp(self):
        self.api = APIClient()
        self.client_obj = Client.objects.create(name="Multi Client")
        self.sku_a = SKU.objects.create(
            description="500ml A", qty_per_case=D("24"), volume_ml=500
        )
        self.sku_b = SKU.objects.create(
            description="1L B", qty_per_case=D("12"), volume_ml=1000
        )
        # Shared raw material: 1 bottle per case of A, 2 per case of B.
        self.bottle = make_material("Bottle shared", "10")
        SKUMaterialRequirement.objects.create(
            sku=self.sku_a, material=self.bottle, qty_per_case=D("1")
        )
        SKUMaterialRequirement.objects.create(
            sku=self.sku_b, material=self.bottle, qty_per_case=D("2")
        )
        make_batch(self.bottle, "30", "10", arrival=date(2026, 8, 1))
        make_batch(self.bottle, "30", "20", arrival=date(2026, 9, 1))
        ClientSKUPrice.objects.create(
            client=self.client_obj, sku=self.sku_a, selling_price_per_case=D("300")
        )
        ClientSKUPrice.objects.create(
            client=self.client_obj, sku=self.sku_b, selling_price_per_case=D("500")
        )

    def bulk(self, lines, **overrides):
        payload = {
            "client": self.client_obj.id,
            "date": "2026-09-20",
            "note": "Route 1",
            "lines": lines,
            **overrides,
        }
        return self.api.post("/api/deliveries/bulk/", payload, format="json")

    def preview(self, lines):
        return self.api.post(
            "/api/deliveries/preview/",
            {"client": self.client_obj.id, "date": "2026-09-20", "lines": lines},
            format="json",
        )

    def test_bulk_saves_one_row_and_ledger_entry_per_sku(self):
        resp = self.bulk(
            [
                {"sku": self.sku_a.id, "qty_cases": "4"},
                {"sku": self.sku_b.id, "qty_cases": "3"},
            ]
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(StockDelivery.objects.count(), 2)
        rows = resp.data["lines"]
        self.assertEqual([r["sku_description"] for r in rows], ["500ml A", "1L B"])
        self.assertEqual(D(rows[0]["qty_cases"]), D("4"))
        self.assertEqual(rows[0]["selling_price_per_case"], "300.00")  # auto-filled
        self.assertEqual(rows[0]["amount"], "1200.00")  # 4 x 300
        self.assertEqual(rows[1]["amount"], "1500.00")  # 3 x 500, auto-filled
        self.assertEqual(D(resp.data["total_cases"]), D("7"))
        self.assertEqual(resp.data["total_amount"], "2700.00")
        self.assertEqual(resp.data["client_pending_amount"], "2700.00")
        self.assertFalse(resp.data["stock_shortfall_flag"])
        self.assertGreater(D(resp.data["cogs"]["per_case"]), D("0"))
        # One ledger entry per SKU line, carrying the delivery's date/note.
        entries = ClientLedgerEntry.objects.filter(entry_type="DELIVERY")
        self.assertEqual(entries.count(), 2)
        self.assertEqual({e.note for e in entries}, {"Route 1"})
        self.assertEqual({e.date for e in entries}, {date(2026, 9, 20)})
        self.assertEqual(
            sum((e.amount for e in entries), D("0")), D("2700.00")
        )

    def test_price_can_be_overridden_per_line(self):
        resp = self.bulk(
            [
                {"sku": self.sku_a.id, "qty_cases": "2", "selling_price_per_case": "250"},
                {"sku": self.sku_b.id, "qty_cases": "1", "selling_price_per_case": "450"},
            ]
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["lines"][0]["amount"], "500.00")
        self.assertEqual(resp.data["lines"][1]["amount"], "450.00")
        self.assertEqual(resp.data["total_amount"], "950.00")

    def test_lines_share_one_fifo_queue_and_exact_stock_is_accepted(self):
        """
        20 cases of A (20 bottles) + 20 cases of B (40 bottles) = the 60
        bottles in stock: accepted (not reported short), and the shared queue
        is walked oldest-first ACROSS the lines.
        """
        resp = self.bulk(
            [
                {"sku": self.sku_a.id, "qty_cases": "20"},
                {"sku": self.sku_b.id, "qty_cases": "20"},
            ]
        )
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.data["stock_shortfall_flag"])
        rows = resp.data["lines"]
        # A only consumes the August batch (20 x 10 = 200 => 10.00/case)
        self.assertEqual(rows[0]["base_cogs_per_case"], "10.00")
        # B then takes the 10 left in August (10 x 10) and 30 from the
        # September batch (30 x 20) = 700 over 20 cases = 35.00/case.
        self.assertEqual(rows[1]["base_cogs_per_case"], "35.00")
        self.assertEqual(
            sum((b.quantity_remaining for b in self.bottle.batches.all()), D("0")),
            D("0"),
        )

    def test_stock_is_checked_for_the_whole_delivery(self):
        """
        Each line fits on its own (A needs 50 of 60, B needs 20 of 60) but the
        delivery as a whole does not (70 > 60) -> 409 with the summed figures,
        nothing saved; "Proceed anyway" (force) saves both lines flagged.
        """
        resp = self.bulk(
            [
                {"sku": self.sku_a.id, "qty_cases": "50"},
                {"sku": self.sku_b.id, "qty_cases": "10"},
            ]
        )
        self.assertEqual(resp.status_code, 409)
        short = resp.data["shortfall"]
        self.assertEqual(len(short), 1)  # one shared material, summed
        self.assertEqual(short[0]["material"], "Bottle shared")
        self.assertEqual(D(short[0]["required"]), D("70"))
        self.assertEqual(D(short[0]["available"]), D("60"))
        self.assertEqual(D(short[0]["short_by"]), D("10"))
        self.assertEqual(StockDelivery.objects.count(), 0)
        self.assertEqual(ClientLedgerEntry.objects.count(), 0)

        resp = self.bulk(
            [
                {"sku": self.sku_a.id, "qty_cases": "50"},
                {"sku": self.sku_b.id, "qty_cases": "10"},
            ],
            force=True,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["stock_shortfall_flag"])
        self.assertEqual(StockDelivery.objects.count(), 2)
        # The flag is per-row (as in the single-delivery flow): line B is the
        # one that drove a batch negative — line A still fitted on its own at
        # its moment of creation. The response carries the aggregate any(...).
        by_sku = {
            d.sku.description: d.stock_shortfall_flag
            for d in StockDelivery.objects.all()
        }
        self.assertEqual(by_sku, {"500ml A": False, "1L B": True})
        self.assertTrue(any(by_sku.values()))

    def test_validation_errors_and_atomicity(self):
        # No lines at all.
        self.assertEqual(
            self.api.post(
                "/api/deliveries/bulk/",
                {"client": self.client_obj.id},
                format="json",
            ).status_code,
            400,
        )
        # Unknown SKU.
        self.assertEqual(
            self.bulk([{"sku": 99999, "qty_cases": "1"}]).status_code,
            400,
        )
        # Same SKU twice is ambiguous -> rejected, nothing saved.
        resp = self.bulk(
            [
                {"sku": self.sku_a.id, "qty_cases": "1"},
                {"sku": self.sku_a.id, "qty_cases": "2"},
            ]
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("listed twice", resp.data["detail"])
        # A bad line later in the list leaves the whole delivery unsaved.
        resp = self.bulk(
            [
                {"sku": self.sku_a.id, "qty_cases": "1"},
                {"sku": self.sku_b.id, "qty_cases": "0"},
            ]
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(StockDelivery.objects.count(), 0)
        self.assertEqual(ClientLedgerEntry.objects.count(), 0)
        # Stock untouched by rejected attempts.
        self.assertEqual(stock_available(self.bottle), D("60"))

    def test_missing_price_is_rejected_for_the_offending_sku(self):
        other = SKU.objects.create(description="Unpriced", qty_per_case=D("12"))
        SKUMaterialRequirement.objects.create(
            sku=other, material=self.bottle, qty_per_case=D("1")
        )
        resp = self.bulk(
            [
                {"sku": self.sku_a.id, "qty_cases": "1"},
                {"sku": other.id, "qty_cases": "1"},
            ]
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No selling price configured for Unpriced", resp.data["detail"])
        self.assertEqual(StockDelivery.objects.count(), 0)

    def test_multi_preview_breaks_down_per_line_without_touching_stock(self):
        resp = self.preview(
            [
                {"sku": self.sku_a.id, "qty_cases": "2"},
                {"sku": self.sku_b.id, "qty_cases": "3"},
            ]
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            [l["sku_description"] for l in resp.data["lines"]],
            ["500ml A", "1L B"],
        )
        self.assertEqual(D(resp.data["totals"]["cases"]), D("5"))
        self.assertEqual(resp.data["shortfall"], [])
        line_a, line_b = resp.data["lines"]
        self.assertEqual(line_a["materials_per_case"], "10.00")
        self.assertEqual(line_b["materials_per_case"], "20.00")
        self.assertEqual(line_a["print_per_case"], "0.00")
        # Nothing was written and FIFO queue is untouched.
        self.assertEqual(StockDelivery.objects.count(), 0)
        self.assertEqual(stock_available(self.bottle), D("60"))

    def test_multi_preview_sums_the_shortfall_per_material(self):
        resp = self.preview(
            [
                {"sku": self.sku_a.id, "qty_cases": "50"},
                {"sku": self.sku_b.id, "qty_cases": "10"},
            ]
        )
        self.assertEqual(resp.status_code, 200)
        short = resp.data["shortfall"]
        self.assertEqual(short[0]["material"], "Bottle shared")
        self.assertEqual(D(short[0]["required"]), D("70"))
        self.assertEqual(D(short[0]["available"]), D("60"))
        self.assertEqual(D(short[0]["short_by"]), D("10"))

    def test_single_sku_preview_backward_compatibility(self):
        resp = self.api.post(
            "/api/deliveries/preview/",
            {
                "client": self.client_obj.id,
                "sku": self.sku_a.id,
                "qty_cases": "2",
                "date": "2026-09-20",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("cogs", resp.data)
        self.assertEqual(resp.data["default_selling_price"], "300.00")
        self.assertIn("shortfall", resp.data)


class DeliveryPaymentAtEntryTests(TestCase):
    """
    User request: a payment can be noted right on the Add-Delivery screen. It is
    OPTIONAL — left blank the delivery is saved on credit exactly as before —
    and when an amount is given it is captured as a normal PAYMENT transaction
    under the client, so the pending balance drops immediately and the money
    shows up in the client's ledger next to the delivery it came with.
    """

    def setUp(self):
        self.api = APIClient()
        self.client_obj = Client.objects.create(name="Paying Client")
        self.sku = SKU.objects.create(
            description="500ml P", qty_per_case=D("24"), volume_ml=500
        )
        self.bottle = make_material("Bottle payable")
        SKUMaterialRequirement.objects.create(
            sku=self.sku, material=self.bottle, qty_per_case=D("1")
        )
        make_batch(self.bottle, "100", "10", arrival=date(2026, 9, 1))
        ClientSKUPrice.objects.create(
            client=self.client_obj, sku=self.sku, selling_price_per_case=D("300")
        )

    def bulk(self, lines, **overrides):
        payload = {
            "client": self.client_obj.id,
            "date": "2026-09-20",
            "note": "Route 1",
            "lines": lines,
            **overrides,
        }
        return self.api.post("/api/deliveries/bulk/", payload, format="json")

    def test_payment_entered_with_delivery_becomes_a_client_transaction(self):
        resp = self.bulk(
            [{"sku": self.sku.id, "qty_cases": "4"}],  # 4 x 300 = 1200
            payment={"amount": "500", "date": "2026-09-20", "note": "Cash"},
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["total_amount"], "1200.00")
        # The response reports the payment and the pending AFTER it.
        self.assertEqual(resp.data["payment"]["amount"], "500.00")
        self.assertEqual(resp.data["payment"]["date"], "2026-09-20")
        self.assertEqual(resp.data["payment"]["note"], "Cash")
        self.assertEqual(resp.data["client_pending_amount"], "700.00")

        payments = ClientLedgerEntry.objects.filter(entry_type="PAYMENT")
        # ONE payment for the whole run, however many SKU lines it carried.
        self.assertEqual(payments.count(), 1)
        entry = payments.get()
        self.assertEqual(entry.client_id, self.client_obj.id)
        self.assertEqual(entry.amount, D("-500"))  # stored negative (spec 3.2)
        self.assertEqual(entry.date, date(2026, 9, 20))
        self.assertEqual(entry.note, "Cash")
        # A multi-SKU run has no single delivery row to point the money at.
        self.assertIsNone(entry.related_delivery_id)
        # …and it is a real line on the client's ledger.
        ledger = self.api.get(f"/api/clients/{self.client_obj.id}/ledger/").data
        self.assertEqual(ledger["pending_amount"], "700.00")
        self.assertEqual(
            [e["entry_type"] for e in ledger["entries"]], ["PAYMENT", "DELIVERY"]
        )

    def test_payment_is_optional_blank_and_zero_mean_no_payment(self):
        resp = self.bulk([{"sku": self.sku.id, "qty_cases": "4"}])
        self.assertEqual(resp.status_code, 201)
        self.assertIsNone(resp.data["payment"])
        self.assertEqual(resp.data["client_pending_amount"], "1200.00")
        self.assertEqual(StockDelivery.objects.count(), 1)
        # Blank / zero amounts coming from the UI must not create an entry
        # either — "optional" has to mean optional in every shape.
        for blank in ({"amount": ""}, {"amount": "0"}, {"amount": None}):
            resp = self.bulk(
                [{"sku": self.sku.id, "qty_cases": "1"}], payment=blank
            )
            self.assertEqual(resp.status_code, 201)
            self.assertIsNone(resp.data["payment"])
        self.assertEqual(
            ClientLedgerEntry.objects.filter(entry_type="PAYMENT").count(), 0
        )
        self.assertEqual(D(self.client_obj.pending_amount), D("2100"))  # 1200 + 3x300


    def test_payment_date_defaults_to_the_delivery_date(self):
        resp = self.bulk(
            [{"sku": self.sku.id, "qty_cases": "1"}], payment={"amount": "300"}
        )
        self.assertEqual(resp.status_code, 201)
        entry = ClientLedgerEntry.objects.get(entry_type="PAYMENT")
        self.assertEqual(entry.date, date(2026, 9, 20))  # the delivery date
        # A default note is written so the ledger row is self-explanatory.
        self.assertIn("Payment with delivery", entry.note)
        self.assertEqual(resp.data["payment"]["date"], "2026-09-20")

    def test_flat_payment_keys_are_accepted_too(self):
        resp = self.bulk(
            [{"sku": self.sku.id, "qty_cases": "1"}],
            payment_amount="300",
            payment_date="2026-09-22",
            payment_note="UPI",
        )
        self.assertEqual(resp.status_code, 201)
        entry = ClientLedgerEntry.objects.get(entry_type="PAYMENT")
        self.assertEqual(entry.amount, D("-300"))
        self.assertEqual(entry.date, date(2026, 9, 22))
        self.assertEqual(entry.note, "UPI")
        self.assertEqual(resp.data["client_pending_amount"], "0.00")

    def test_single_sku_delivery_links_the_payment_to_the_delivery(self):
        resp = self.api.post(
            "/api/deliveries/",
            {
                "client": self.client_obj.id,
                "sku": self.sku.id,
                "qty_cases": "2",  # 2 x 300 = 600
                "date": "2026-09-20",
                "payment": {"amount": "100", "date": "2026-09-21"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        entry = ClientLedgerEntry.objects.get(entry_type="PAYMENT")
        # Single-SKU delivery -> the money points at the delivery it came with.
        self.assertEqual(entry.related_delivery_id, resp.data["delivery"]["id"])
        self.assertEqual(entry.date, date(2026, 9, 21))  # dated by the user
        self.assertEqual(resp.data["payment"]["amount"], "100.00")
        self.assertEqual(resp.data["client_pending_amount"], "500.00")

    def test_a_payment_larger_than_the_delivery_is_an_advance(self):
        # Nothing blocks the client paying more than this delivery is worth —
        # it just reduces (here: over-reduces) the running pending balance.
        resp = self.bulk(
            [{"sku": self.sku.id, "qty_cases": "1"}],  # 300
            payment={"amount": "500", "note": "Advance"},
        )
        self.assertEqual(resp.status_code, 201)
        self.assertIsNotNone(resp.data["payment"])
        self.assertEqual(resp.data["client_pending_amount"], "-200.00")
        self.assertEqual(D(self.client_obj.pending_amount), D("-200"))

    def test_bad_payment_amount_rejects_the_whole_delivery(self):
        for bad in ("abc", "-5"):
            resp = self.bulk(
                [{"sku": self.sku.id, "qty_cases": "1"}], payment={"amount": bad}
            )
            self.assertEqual(resp.status_code, 400)
        # Atomic: no delivery, no ledger entry, no consumed stock survived.
        self.assertEqual(StockDelivery.objects.count(), 0)
        self.assertEqual(ClientLedgerEntry.objects.count(), 0)
        self.assertEqual(stock_available(self.bottle), D("100"))

        # A malformed payment date is rejected just as loudly.
        resp = self.bulk(
            [{"sku": self.sku.id, "qty_cases": "1"}],
            payment={"amount": "100", "date": "20-09-2026"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Payment date", resp.data["detail"])
        self.assertEqual(StockDelivery.objects.count(), 0)
        self.assertEqual(stock_available(self.bottle), D("100"))


class BackfillNegativeStockCommandTests(TestCase):
    """
    Approved fix-up tool: deliveries recorded BEFORE the negative-stock rule
    could leave their shortfall unrecorded (stock stopped at 0 instead of
    showing the true negative figure, or nothing was booked when the material
    had no batches at all). `backfill_negative_stock` reports those gaps
    read-only and books them only with --apply.
    """

    def setUp(self):
        self.client_obj = Client.objects.create(name="Legacy Co")
        self.sku = SKU.objects.create(
            description="Legacy 500ml", qty_per_case=D("24"), volume_ml=500
        )
        self.bottle = make_material("Legacy Bottle", "240")
        SKUMaterialRequirement.objects.create(
            sku=self.sku, material=self.bottle, qty_per_case=D("1")
        )
        ClientSKUPrice.objects.create(
            client=self.client_obj, sku=self.sku, selling_price_per_case=D("300")
        )

    def _legacy_exhausted_queue(self, qty="15"):
        """
        Pre-fix state: 10 cases in stock, a 15-case delivery consumed the
        positive 10 and the excess 5 was DROPPED (stock stopped at 0) — today
        the same delivery books -5 on the batch.
        """
        batch = make_batch(self.bottle, "10", "240")
        create_delivery(
            self.client_obj,
            self.sku,
            D(qty),
            delivery_date=date(2026, 9, 5),
            force=True,
        )
        batch.refresh_from_db()
        batch.quantity_remaining = D("0")
        batch.save(skip_audit=True)
        self.assertEqual(self.bottle.stock_in_hand, D("0"))
        return batch

    def _run(self, *args):
        out = StringIO()
        call_command("backfill_negative_stock", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_the_gap_and_writes_nothing(self):
        batch = self._legacy_exhausted_queue()
        report = self._run()
        self.assertIn("DRY RUN", report)
        self.assertIn("Legacy Bottle", report)
        self.assertIn("unrecorded shortfall 5", report)
        self.assertIn(f"would push the oldest batch #{batch.id}", report)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_remaining, D("0"))
        self.assertEqual(self.bottle.batches.count(), 1)
        self.assertEqual(StockDelivery.objects.count(), 1)

    def test_apply_books_the_gap_on_the_oldest_batch_and_is_idempotent(self):
        batch = self._legacy_exhausted_queue()
        report = self._run("--apply")
        self.assertIn("[APPLY]", report)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_remaining, D("-5"))  # 15 - 10
        self.assertEqual(self.bottle.stock_in_hand, D("-5"))
        # Booking the gap raised "already booked" by exactly the gap, so a
        # second pass finds nothing and changes nothing.
        again = self._run("--apply")
        self.assertIn("No unrecorded stock shortfalls", again)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_remaining, D("-5"))
        self.assertEqual(self.bottle.batches.count(), 1)

    def test_apply_creates_a_deficit_carrier_when_there_are_no_batches(self):
        StockDelivery.objects.create(
            client=self.client_obj,
            sku=self.sku,
            qty_cases=D("10"),
            selling_price_per_case=D("300"),
            date=date(2026, 9, 5),
        )
        self.assertIn("would create a zero-received deficit carrier batch", self._run())
        report = self._run("--apply")
        carrier = self.bottle.batches.get()
        self.assertEqual(carrier.quantity_received, D("0"))  # not an arrival
        self.assertEqual(carrier.quantity_remaining, D("-10"))
        self.assertEqual(carrier.arrival_date, date(2026, 9, 5))  # delivery date
        self.assertEqual(carrier.price_per_unit, D("240"))  # master display price
        self.assertEqual(self.bottle.stock_in_hand, D("-10"))
        self.assertIn("deficit carrier batch", report)

    def test_healthy_ledger_reports_nothing_and_apply_changes_nothing(self):
        make_batch(self.bottle, "10", "240")
        create_delivery(
            self.client_obj, self.sku, D("6"), delivery_date=date(2026, 9, 5)
        )
        report = self._run("--apply")
        self.assertIn("No unrecorded stock shortfalls", report)
        self.assertEqual(self.bottle.batches.count(), 1)
        self.assertEqual(self.bottle.stock_in_hand, D("4"))

    def test_soft_deleted_delivery_is_not_counted_as_demand(self):
        batch = self._legacy_exhausted_queue()
        StockDelivery.objects.get().soft_delete()  # user removed the delivery
        report = self._run("--apply")
        # The deleted delivery no longer creates a demand, so the 10 already
        # booked turns the gap negative — nothing is booked on the ledger.
        self.assertIn("No unrecorded stock shortfalls", report)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_remaining, D("0"))
        self.assertEqual(self.bottle.batches.count(), 1)

class RequirementlessSkuStockTests(TestCase):
    """
    User report: "stock shows 0 and does not go negative even after registering
    the deliveries".

    Root cause: the deliveries were for SKUs that carry NO material
    requirements, so FIFO consumed nothing at all — no batch could be pushed
    negative and the shortfall check could never fire, leaving every screen
    silent. The FIFO/negative-stock engine itself is correct (see
    NegativeStockMarkingTests); these tests pin the warning that now NAMES the
    requirement-less SKUs, plus the guard that stops a zero/negative per-case
    requirement from being saved in the first place.
    """

    def setUp(self):
        self.api = APIClient()
        self.client_obj = Client.objects.create(name="Reported Client")
        self.bottle = make_material("Bottle 500ml", "10")
        make_batch(self.bottle, "50", "10", arrival=date(2026, 9, 1))
        # The reported case: a SKU with no linked materials at all.
        self.sku_plain = SKU.objects.create(
            description="500ml no materials", qty_per_case=D("24"), volume_ml=500
        )
        self.sku_linked = SKU.objects.create(
            description="500ml linked", qty_per_case=D("24"), volume_ml=500
        )
        SKUMaterialRequirement.objects.create(
            sku=self.sku_linked, material=self.bottle, qty_per_case=D("1")
        )
        for sku in (self.sku_plain, self.sku_linked):
            ClientSKUPrice.objects.create(
                client=self.client_obj,
                sku=sku,
                selling_price_per_case=D("300"),
            )

    def bulk(self, lines, **overrides):
        payload = {
            "client": self.client_obj.id,
            "date": "2026-09-20",
            "lines": lines,
            **overrides,
        }
        return self.api.post("/api/deliveries/bulk/", payload, format="json")

    def preview(self, lines):
        return self.api.post(
            "/api/deliveries/preview/",
            {"client": self.client_obj.id, "date": "2026-09-20", "lines": lines},
            format="json",
        )
    def test_delivery_for_requirementless_sku_moves_no_stock_and_says_so(self):
        resp = self.bulk([{"sku": self.sku_plain.id, "qty_cases": "5"}])
        self.assertEqual(resp.status_code, 201)
        # The blind spot is now named in the response.
        self.assertTrue(resp.data["no_material_requirements"])
        self.assertEqual(
            resp.data["skus_without_requirements"], ["500ml no materials"]
        )
        # No consumption, so no negative stock and no shortfall could fire.
        self.assertFalse(resp.data["stock_shortfall_flag"])
        self.assertEqual(resp.data["lines"][0]["base_cogs_per_case"], "0.00")
        self.assertEqual(self.bottle.stock_in_hand, D("50"))

    def test_preview_names_the_requirementless_line_only(self):
        resp = self.preview(
            [
                {"sku": self.sku_plain.id, "qty_cases": "5"},
                {"sku": self.sku_linked.id, "qty_cases": "4"},
            ]
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["no_material_requirements"])
        self.assertEqual(
            resp.data["skus_without_requirements"], ["500ml no materials"]
        )
        self.assertEqual(resp.data["shortfall"], [])
        plain, linked = resp.data["lines"]
        self.assertTrue(plain["no_material_requirements"])
        self.assertFalse(linked["no_material_requirements"])
        self.assertEqual(plain["details"]["materials"], [])
        self.assertEqual(len(linked["details"]["materials"]), 1)

    def test_linked_sku_is_not_flagged_and_still_consumes_stock(self):
        resp = self.preview([{"sku": self.sku_linked.id, "qty_cases": "4"}])
        self.assertFalse(resp.data["no_material_requirements"])
        self.assertEqual(resp.data["skus_without_requirements"], [])

        resp = self.bulk([{"sku": self.sku_linked.id, "qty_cases": "4"}])
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.data["no_material_requirements"])
        self.assertEqual(resp.data["skus_without_requirements"], [])
        self.assertEqual(self.bottle.stock_in_hand, D("46"))

    def test_single_sku_paths_carry_the_same_flag(self):
        resp = self.api.post(
            "/api/deliveries/",
            {
                "client": self.client_obj.id,
                "sku": self.sku_plain.id,
                "qty_cases": "3",
                "date": "2026-09-20",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["no_material_requirements"])
        self.assertEqual(
            resp.data["skus_without_requirements"], ["500ml no materials"]
        )
        self.assertEqual(resp.data["cogs"]["details"]["materials"], [])
        self.assertEqual(self.bottle.stock_in_hand, D("50"))

        resp = self.api.post(
            "/api/deliveries/preview/",
            {
                "client": self.client_obj.id,
                "sku": self.sku_plain.id,
                "qty_cases": "3",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["no_material_requirements"])

    def test_requirement_quantity_must_be_positive(self):
        for bad in ("0", "-2"):
            resp = self.api.post(
                "/api/sku-requirements/",
                {
                    "sku": self.sku_plain.id,
                    "material": self.bottle.id,
                    "qty_per_case": bad,
                },
                format="json",
            )
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIn("greater than zero", str(resp.data["qty_per_case"]))
        # Nothing was saved: the SKU is still requirement-less.
        self.assertFalse(self.sku_plain.requirements.exists())

        resp = self.api.post(
            "/api/sku-requirements/",
            {
                "sku": self.sku_plain.id,
                "material": self.bottle.id,
                "qty_per_case": "1",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self.sku_plain.requirements.count(), 1)








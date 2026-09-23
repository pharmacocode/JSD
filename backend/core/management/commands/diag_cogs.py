from django.core.management.base import BaseCommand
from decimal import Decimal
from core.models import StockDelivery, SKUMaterialRequirement
from core import cogs


def handle(self):
    d = StockDelivery.objects.get(id=2)
    print("date", d.date, "qty", d.qty_cases,
          "oh_pool", cogs.overhead_for_month(cogs.month_key(d.date)),
          "oh_per_case", cogs.overhead_per_case(cogs.month_key(d.date), d.qty_cases))
    raw = Decimal(0)
    qty = d.qty_cases
    for req_row in SKUMaterialRequirement.objects.filter(sku=d.sku):
        m = req_row.material
        per_case_qty = req_row.qty_per_case
        required = qty * per_case_qty
        wf = Decimal("1") + m.wastage_percent / Decimal("100")
        uc = cogs._preview_fifo_unit_cost(m, required)
        lc = required * uc * wf
        raw += lc
        print("{0} unit={1} req={2} cost={3}".format(m.name, uc, required, lc))
    raw_per_case = raw / qty if qty else 0
    pc, _ = cogs.print_cost_per_case(d.sku)
    print("raw_per_case", raw_per_case, "print_per_case", pc, "base", raw_per_case + pc)


class Command(BaseCommand):
    def handle(self, *args, **options):
        return handle(self)
"""
Keep the perf memo honest: any write to a model it reads drops it immediately.

`perfcache` memoizes cost/stock queries for the duration of one request.
Within that request a write (create delivery, edit a batch, add an overhead)
must be visible to everything computed afterwards — e.g. POST /deliveries/
serialises the new row with costs computed AFTER the row was saved. The memo
therefore bumps its generation on every save/delete of the models those
helpers read, so no caller has to remember to invalidate a specific key.

Registering one handler per model (rather than one global handler) keeps the
blast radius explicit: a model not listed here cannot silently go stale.
"""

from django.db.models.signals import post_delete, post_save

from . import perfcache
from .models import (
    Client,
    ClientLedgerEntry,
    ClientOrder,
    Material,
    MaterialBatch,
    MonthlyOverhead,
    OrderItem,
    SKU,
    SKUMaterialRequirement,
    SKUPrintCost,
    StockAdjustment,
    StockDelivery,
)

# Every model whose rows feed a memoized helper in cogs.py / models.py:
#   resolve_requirements        -> SKU, SKUMaterialRequirement, Material
#   print_cost_per_case         -> SKUPrintCost, SKU
#   stock_available / FIFO snap -> MaterialBatch, Material
#   overhead_for_month          -> MonthlyOverhead
#   cases_sold_in_month         -> StockDelivery
#   legacy_demand_by_material   -> StockDelivery, StockAdjustment
#   legacy_booked_consumption   -> MaterialBatch
#   committed_map / available   -> OrderItem (status, qty, delivery link)
#   Client.pending_amount       -> Client, ClientLedgerEntry
MONITORED = (
    Client,
    ClientLedgerEntry,
    ClientOrder,
    Material,
    MaterialBatch,
    MonthlyOverhead,
    OrderItem,
    SKU,
    SKUMaterialRequirement,
    SKUPrintCost,
    StockAdjustment,
    StockDelivery,
)


def _invalidate(sender, **kwargs):
    perfcache.invalidate()


for _model in MONITORED:
    post_save.connect(
        _invalidate, sender=_model, dispatch_uid=f"perfcache_save_{_model.__name__}"
    )
    post_delete.connect(
        _invalidate, sender=_model, dispatch_uid=f"perfcache_del_{_model.__name__}"
    )
"""Composite indexes for the hot read paths (perf plan, Phase 1.5).

The serializers/dashboard scope every heavy query by a date range (or an
arrival-date range) plus an `is_deleted` soft-delete guard, and the FIFO /
ledger walks are keyed by the parent FK. These composite indexes let the
database serve those filters and sort orders directly instead of scanning
whole tables — the difference only widens as the tables grow.

Names are explicit so the model `Meta.indexes` and this migration can never
drift apart.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_stockdelivery_consumption_log"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="stockdelivery",
            index=models.Index(
                fields=["date", "is_deleted"],
                name="delivery_date_isdeleted_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="materialbatch",
            index=models.Index(
                fields=["material", "arrival_date", "is_deleted"],
                name="matbatch_arrival_isdeleted_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="clientledgerentry",
            index=models.Index(
                fields=["client", "date", "is_deleted"],
                name="ledger_client_date_idx",
            ),
        ),
    ]

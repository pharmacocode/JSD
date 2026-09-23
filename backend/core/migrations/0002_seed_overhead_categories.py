from django.db import migrations

DEFAULT_CATEGORIES = ["Rent", "Diesel", "Electricity", "Labour"]


def seed_defaults(apps, schema_editor):
    OverheadCategory = apps.get_model("core", "OverheadCategory")
    for name in DEFAULT_CATEGORIES:
        OverheadCategory.objects.get_or_create(
            name=name, defaults={"is_default": True}
        )


def unseed_defaults(apps, schema_editor):
    OverheadCategory = apps.get_model("core", "OverheadCategory")
    OverheadCategory.objects.filter(name__in=DEFAULT_CATEGORIES).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        # Spec 3.10: Rent, Diesel, Electricity, Labour pre-seeded as
        # non-deletable defaults.
        migrations.RunPython(seed_defaults, unseed_defaults),
    ]

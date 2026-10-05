from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        # Wire the perf-memo invalidation signals (any write to a model the
        # memo reads drops it — see core/signals.py).
        from . import signals  # noqa: F401

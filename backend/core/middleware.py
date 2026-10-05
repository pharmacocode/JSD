"""Middleware: scope the perf memo to exactly one request (see core/perfcache)."""

from . import perfcache


class PerfCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        perfcache.clear_request_cache()
        return self.get_response(request)
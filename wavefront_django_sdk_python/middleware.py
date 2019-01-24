import os
import time
import math
import logging
from timeit import default_timer
from django.urls import resolve
from django.conf import settings
from django_opentracing import DjangoTracing
from django_opentracing.tracing import initialize_global_tracer
from wavefront_pyformance.wavefront_reporter import WavefrontReporter
from wavefront_pyformance.tagged_registry import TaggedRegistry
from wavefront_pyformance.delta import delta_counter
from wavefront_pyformance.wavefront_histogram import wavefront_histogram
from wavefront_sdk.common import HeartbeaterService, ApplicationTags
from wavefront_django_sdk_python.constants import NULL_TAG_VAL, \
    WAVEFRONT_PROVIDED_SOURCE, RESPONSE_PREFIX, REQUEST_PREFIX, \
    REPORTER_PREFIX, DJANGO_COMPONENT, HEART_BEAT_INTERVAL

try:
    # Django >= 1.10
    from django.utils.deprecation import MiddlewareMixin
except ImportError:
    # Not required for Django <= 1.9, see:
    MiddlewareMixin = object


class WavefrontMiddleware(MiddlewareMixin):

    def __init__(self, get_response=None):
        super(WavefrontMiddleware, self).__init__(get_response)
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
        self.MIDDLEWARE_ENABLED = False
        try:
            self.reporter = self.get_conf('WF_REPORTER')
            self.application_tags = self.get_conf('APPLICATION_TAGS')
            self.tracing = self.get_conf('OPENTRACING_TRACING')
            if not isinstance(self.reporter, WavefrontReporter):
                raise AttributeError(
                    "WF_REPORTER not correctly configured!")
            elif not isinstance(self.application_tags, ApplicationTags):
                raise AttributeError(
                    "APPLICATION_TAGS not correctly configured!")
            elif not isinstance(self.tracing, DjangoTracing):
                raise AttributeError(
                    "OPENTRACING_TRACING not correctly configured!")
            else:
                self.APPLICATION = self.application_tags.application or \
                                   NULL_TAG_VAL
                self.CLUSTER = self.application_tags.cluster or NULL_TAG_VAL
                self.SERVICE = self.application_tags.service or NULL_TAG_VAL
                self.SHARD = self.application_tags.shard or NULL_TAG_VAL
                self.reporter.prefix = REPORTER_PREFIX
                self.reg = TaggedRegistry()
                self.reporter.registry = self.reg
                self.reporter.start()
                self.heartbeaterService = HeartbeaterService(
                    wavefront_client=self.reporter.wavefront_client,
                    application_tags=self.application_tags,
                    component=DJANGO_COMPONENT,
                    source=self.reporter.source,
                    reporting_interval_seconds=HEART_BEAT_INTERVAL)
                initialize_global_tracer(self.tracing)
                self.MIDDLEWARE_ENABLED = True
        except AttributeError as e:
            self.logger.warning(e)
        finally:
            if not self.MIDDLEWARE_ENABLED:
                self.logger.warning("Wavefront Django Middleware not enabled!")

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not self.MIDDLEWARE_ENABLED:
            return
        request.wf_start_timestamp = default_timer()
        request.wf_cpu_nanos = time.clock()

        entity_name = self.get_entity_name(request)
        func_name = resolve(request.path_info).func.__name__
        module_name = resolve(request.path_info).func.__module__
        self.update_gauge(
            registry=self.reg,
            key=self.get_metric_name(entity_name, request) + ".inflight",
            tags=self.get_tags_map(
                module_name=module_name,
                func_name=func_name),
            val=1
        )
        self.update_gauge(
            registry=self.reg,
            key="total_requests.inflight",
            tags=self.get_tags_map(
                cluster=self.CLUSTER,
                service=self.SERVICE,
                shard=self.SHARD),
            val=1
        )
        if self.tracing:
            if not self.tracing._trace_all:
                return None
            if hasattr(settings, 'OPENTRACING_TRACED_ATTRIBUTES'):
                traced_attributes = getattr(settings,
                                            'OPENTRACING_TRACED_ATTRIBUTES')
            else:
                traced_attributes = []
            self.tracing._apply_tracing(request, view_func, traced_attributes)

    def process_response(self, request, response):
        if not self.MIDDLEWARE_ENABLED:
            return response
        entity_name = self.get_entity_name(request)
        func_name = resolve(request.path_info).func.__name__
        module_name = resolve(request.path_info).func.__module__

        if self.tracing:
            span = self.tracing.get_span(request)
            span.set_tag("http.status_code", str(response.status_code))
            if self.is_error_status_code(response):
                span.set_tag("error", "true")
            span.set_tag("span.kind", "server")
            span.set_tag("django.resource.module", module_name)
            span.set_tag("django.resource.func", func_name)
            span.set_tag("component", DJANGO_COMPONENT)
            span.set_tag("http.method", request.method)
            span.set_tag("http.url", request.build_absolute_uri())
            self.tracing._finish_tracing(request, response=response)

        self.update_gauge(
            registry=self.reg,
            key=self.get_metric_name(entity_name, request) + ".inflight",
            tags=self.get_tags_map(
                module_name=module_name,
                func_name=func_name),
            val=-1
        )
        self.update_gauge(
            registry=self.reg,
            key="total_requests.inflight",
            tags=self.get_tags_map(
                cluster=self.CLUSTER,
                service=self.SERVICE,
                shard=self.SHARD),
            val=-1
        )

        response_metric_key = self.get_metric_name(entity_name, request,
                                                   response)

        complete_tags_map = self.get_tags_map(
            cluster=self.CLUSTER,
            service=self.SERVICE,
            shard=self.SHARD,
            module_name=module_name,
            func_name=func_name
        )

        aggregated_per_shard_map = self.get_tags_map(
            cluster=self.CLUSTER,
            service=self.SERVICE,
            shard=self.SHARD,
            module_name=module_name,
            func_name=func_name,
            source=WAVEFRONT_PROVIDED_SOURCE)

        overall_aggregated_per_source_map = self.get_tags_map(
            cluster=self.CLUSTER,
            service=self.SERVICE,
            shard=self.SHARD)

        overall_aggregated_per_shard_map = self.get_tags_map(
            cluster=self.CLUSTER,
            service=self.SERVICE,
            shard=self.SHARD,
            source=WAVEFRONT_PROVIDED_SOURCE)

        aggregated_per_service_map = self.get_tags_map(
            cluster=self.CLUSTER,
            service=self.SERVICE,
            module_name=module_name,
            func_name=func_name,
            source=WAVEFRONT_PROVIDED_SOURCE)

        overall_aggregated_per_service_map = self.get_tags_map(
            cluster=self.CLUSTER,
            service=self.SERVICE,
            source=WAVEFRONT_PROVIDED_SOURCE)

        aggregated_per_cluster_map = self.get_tags_map(
            cluster=self.CLUSTER,
            module_name=module_name,
            func_name=func_name,
            source=WAVEFRONT_PROVIDED_SOURCE)

        overall_aggregated_per_cluster_map = self.get_tags_map(
            cluster=self.CLUSTER,
            source=WAVEFRONT_PROVIDED_SOURCE)

        aggregated_per_application_map = self.get_tags_map(
            module_name=module_name,
            func_name=func_name,
            source=WAVEFRONT_PROVIDED_SOURCE
        )

        overall_aggregated_per_application_map = self.get_tags_map(
            source=WAVEFRONT_PROVIDED_SOURCE)

        # django.server.response.style._id_.make.GET.200.cumulative.count
        # django.server.response.style._id_.make.GET.200.aggregated_per_shard.count
        # django.server.response.style._id_.make.GET.200.aggregated_per_service.count
        # django.server.response.style._id_.make.GET.200.aggregated_per_cluster.count
        # django.server.response.style._id_.make.GET.200.aggregated_per_application.count
        # django.server.response.style._id_.make.GET.errors
        self.reg.counter(response_metric_key + ".cumulative",
                         tags=complete_tags_map).inc()
        if self.application_tags.shard:
            delta_counter(
                self.reg, response_metric_key + ".aggregated_per_shard",
                tags=aggregated_per_shard_map).inc()
        delta_counter(
            self.reg, response_metric_key + ".aggregated_per_service",
            tags=aggregated_per_service_map).inc()
        if self.application_tags.cluster:
            delta_counter(
                self.reg, response_metric_key + ".aggregated_per_cluster",
                tags=aggregated_per_cluster_map).inc()
        delta_counter(
            self.reg, response_metric_key + ".aggregated_per_application",
            tags=aggregated_per_application_map).inc()

        # django.server.response.errors.aggregated_per_source.count
        # django.server.response.errors.aggregated_per_shard.count
        # django.server.response.errors.aggregated_per_service.count
        # django.server.response.errors.aggregated_per_cluster.count
        # django.server.response.errors.aggregated_per_application.count
        if self.is_error_status_code(response):
            self.reg.counter(
                self.get_metric_name_without_status(entity_name, request),
                tags=complete_tags_map).inc()
            self.reg.counter("response.errors", tags=complete_tags_map).inc()
            self.reg.counter("response.errors.aggregated_per_source",
                             tags=overall_aggregated_per_source_map).inc()
            if self.application_tags.shard:
                delta_counter(self.reg, "response.errors.aggregated_per_shard",
                              tags=overall_aggregated_per_shard_map).inc()
            delta_counter(self.reg, "response.errors.aggregated_per_service",
                          tags=overall_aggregated_per_service_map).inc()
            if self.application_tags.cluster:
                delta_counter(self.reg,
                              "response.errors.aggregated_per_cluster",
                              tags=overall_aggregated_per_cluster_map).inc()
            delta_counter(self.reg,
                          "response.errors.aggregated_per_application",
                          tags=overall_aggregated_per_application_map).inc()

        # django.server.response.completed.aggregated_per_source.count
        # django.server.response.completed.aggregated_per_shard.count
        # django.server.response.completed.aggregated_per_service.count
        # django.server.response.completed.aggregated_per_cluster.count
        # django.server.response.completed.aggregated_per_application.count
        self.reg.counter("response.completed.aggregated_per_source",
                         tags=overall_aggregated_per_source_map).inc()
        if self.SHARD is not NULL_TAG_VAL:
            delta_counter(
                self.reg, "response.completed.aggregated_per_shard",
                tags=overall_aggregated_per_shard_map).inc()
            self.reg.counter("response.completed.aggregated_per_service",
                             tags=overall_aggregated_per_service_map).inc()
        if self.CLUSTER is not NULL_TAG_VAL:
            delta_counter(
                self.reg, "response.completed.aggregated_per_cluster",
                tags=overall_aggregated_per_cluster_map).inc()
            self.reg.counter("response.completed.aggregated_per_application",
                             tags=overall_aggregated_per_application_map).inc()

        # django.server.response.style._id_.make.summary.GET.200.latency.m
        # django.server.response.style._id_.make.summary.GET.200.cpu_ns.m
        if hasattr(request, 'wf_start_timestamp'):
            timestamp_duration = default_timer() - request.wf_start_timestamp
            cpu_nanos_duration = time.clock() - request.wf_cpu_nanos
            wavefront_histogram(self.reg, response_metric_key + ".latency",
                                tags=complete_tags_map).add(timestamp_duration)
            wavefront_histogram(self.reg, response_metric_key + ".cpu_ns",
                                tags=complete_tags_map).add(cpu_nanos_duration)
        return response

    @staticmethod
    def get_tags_map(cluster=None, service=None, shard=None, module_name=None,
                     func_name=None, source=None):
        tags_map = {}
        if cluster:
            tags_map['cluster'] = cluster
        if service:
            tags_map['service'] = service
        if shard:
            tags_map['shard'] = shard
        if module_name:
            tags_map['django.resource.module'] = module_name
        if func_name:
            tags_map['django.resource.func'] = func_name
        if source:
            tags_map['source'] = source
        return tags_map

    @staticmethod
    def get_entity_name(request):
        resolver_match = request.resolver_match
        if resolver_match:
            entity_name = resolver_match.url_name
            if not entity_name:
                entity_name = resolver_match.view_name
            entity_name = entity_name.replace('-', '_').replace('/', '.'). \
                replace('{', '_').replace('}', '_')
        else:
            entity_name = 'UNKNOWN'
        return entity_name.lstrip('.').rstrip('.')

    @staticmethod
    def get_metric_name(entity_name, request, response=None):
        metric_name = [entity_name, request.method]
        if response:
            metric_name.insert(0, RESPONSE_PREFIX)
            metric_name.append(str(response.status_code))
        else:
            metric_name.insert(0, REQUEST_PREFIX)
        return '.'.join(metric_name)

    @staticmethod
    def get_metric_name_without_status(entity_name, request):
        metric_name = [entity_name, request.method]
        metric_name.insert(0, REQUEST_PREFIX)
        return '.'.join(metric_name)

    @staticmethod
    def is_error_status_code(response):
        return 400 <= response.status_code <= 599

    @staticmethod
    def update_gauge(registry, key, tags, val):
        gauge = registry.gauge(key=key, tags=tags)
        cur_val = gauge.get_value()
        if math.isnan(cur_val):
            cur_val = 0
        gauge.set_value(cur_val + val)

    @staticmethod
    def get_conf(key):
        if hasattr(settings, key):
            return settings.__getattr__(key)
        if key in os.environ:
            return os.environ[key]
        return None

"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.api_provider import ApiProviderService
from app.services.auth_profile import AuthProfileService
from app.services.endpoint_def import EndpointDefService
from app.services.rate_limit import RateLimitService
from app.services.request_batch import RequestBatchService
from app.services.response_parse import ResponseParseService
from app.services.schema_diff import SchemaDiffService
from app.services.data_cache import DataCacheService
from app.services.retry_queue import RetryQueueService
from app.services.data_delivery import DataDeliveryService
from app.services.subscriber import SubscriberService
from app.services.api_monitor import ApiMonitorService
from app.services.webhook_router import WebhookRouterService
from app.services.data_encrypt import DataEncryptService
from app.services.call_log import CallLogService
from app.services.data_version import DataVersionService
from app.services.pipeline_audit import PipelineAuditService
from app.services.govern_report import GovernReportService
from app.services.incr_tracker import IncrTrackerService
from app.services.incr_breakpoint import IncrBreakpointService
from app.services.cursor_migration import CursorMigrationService
from app.services.sample_check import SampleCheckService
from app.services.cost_account import CostAccountService
from app.services.task_schedule import TaskScheduleService

SERVICES: dict[str, BaseModuleService] = {
    "api_provider": ApiProviderService(),
    "auth_profile": AuthProfileService(),
    "endpoint_def": EndpointDefService(),
    "rate_limit": RateLimitService(),
    "request_batch": RequestBatchService(),
    "response_parse": ResponseParseService(),
    "schema_diff": SchemaDiffService(),
    "data_cache": DataCacheService(),
    "retry_queue": RetryQueueService(),
    "data_delivery": DataDeliveryService(),
    "subscriber": SubscriberService(),
    "api_monitor": ApiMonitorService(),
    "webhook_router": WebhookRouterService(),
    "data_encrypt": DataEncryptService(),
    "call_log": CallLogService(),
    "data_version": DataVersionService(),
    "pipeline_audit": PipelineAuditService(),
    "govern_report": GovernReportService(),
    "incr_tracker": IncrTrackerService(),
    "incr_breakpoint": IncrBreakpointService(),
    "cursor_migration": CursorMigrationService(),
    "sample_check": SampleCheckService(),
    "cost_account": CostAccountService(),
    "task_schedule": TaskScheduleService(),
}


SERVICE_REGISTRY = SERVICES


def service_for(module: str) -> BaseModuleService | None:
    return SERVICES.get(module)

"""领域异常 → HTTP 状态码的统一映射。

把「业务错误」和「系统错误」分开，是算法服务化里很重要的一层：
上游调用方需要能区分“我传错了”“你忙”“你内部挂了”，从而决定重试还是告警。
"""
from __future__ import annotations


class PlatformError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class BadRequest(PlatformError):
    status_code = 400
    code = "bad_request"


class NotFound(PlatformError):
    status_code = 404
    code = "not_found"


class PayloadTooLarge(PlatformError):
    status_code = 413
    code = "payload_too_large"


class UnsupportedMedia(PlatformError):
    status_code = 415
    code = "unsupported_media_type"


class QueueSaturated(PlatformError):
    """队列已满，属于可重试的背压信号。"""

    status_code = 429
    code = "queue_saturated"


class Conflict(PlatformError):
    status_code = 409
    code = "conflict"


class RetryableJobError(Exception):
    """算法执行中的瞬时错误，worker 会按指数退避重试。"""

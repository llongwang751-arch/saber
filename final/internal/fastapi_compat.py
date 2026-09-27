# fastapi_compat — FastAPI 新旧版本兼容层。
#
# 1) 生命周期：旧的 app.add_event_handler("shutdown", ...) 在 FastAPI 0.116+
#    被移除（Starlette 1.x 连 on_startup/on_shutdown 列表也一并删除），
#    统一改为 lifespan 退出时按注册顺序调用关闭回调，新旧 FastAPI 均兼容。
# 2) 路由内省：FastAPI 0.14x 起 include_router 不再把子路由平铺进 app.routes，
#    而是挂一个 _IncludedRouter 包装对象；iter_all_routes 递归展开包装，
#    让依赖 app.routes 的代码（OpenAPI 定制、契约测试）在两个版本上都可用。
# 本模块不得 import internal.handler / internal.application（避免循环导入）。
import inspect
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable, Iterator, List

logger = logging.getLogger(__name__)


@asynccontextmanager
async def app_lifespan(app):
    try:
        yield
    finally:
        for callback in list(getattr(app.state, "shutdown_callbacks", [])):
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                logger.warning("⚠️  shutdown 回调失败: %s", e)


def register_shutdown(app, callback: Callable[[], Any]) -> None:
    """注册应用关闭回调；等价于旧的 add_event_handler("shutdown", fn)。"""
    callbacks: List[Callable[[], Any]] = getattr(app.state, "shutdown_callbacks", None)
    if callbacks is None:
        callbacks = []
        app.state.shutdown_callbacks = callbacks
    callbacks.append(callback)


def iter_all_routes(routes) -> Iterator[Any]:
    """递归展开 include_router 产生的嵌套结构，yield 最终的 Route 对象。

    新版 FastAPI 的 _IncludedRouter 暴露 effective_route_contexts()，其中
    starlette_route 已应用 include 前缀；旧版路由表本身是平铺的，原样返回。
    """
    for route in routes:
        effective = getattr(route, "effective_route_contexts", None)
        if callable(effective):
            for context in effective():
                resolved = getattr(context, "starlette_route", None) or getattr(
                    context, "original_route", None
                )
                if resolved is not None:
                    yield resolved
                else:
                    yield context
            continue
        nested = getattr(route, "routes", None)
        if nested is not None and not getattr(route, "methods", None):
            yield from iter_all_routes(nested)
            continue
        yield route

"""每个 web 实例的进程内轻量指标:计数器 / 计时 / 仪表,零第三方依赖。

设计约束(项目"零新增依赖"):
- 只用标准库;所有方法纯内存 + 单锁,埋点调用失败不可能破坏业务路径
- 不做 label 基数机制:counter 是扁平名,状态码等维度在调用点拼进名字
  (如 http_status_423)
- 计时只记 count 与 sum(可求平均),不做 histogram 分桶
- 仪表(gauge)两类:进程内 gauge_add 直接维护(如 llm_slots_in_use);
  外部资源占用(PG 连接池)经 provider 注册,快照时惰性求值,provider
  抛异常一律吞掉——观测永不影响快照可用性
- 生产 Prometheus/OTel 路径:抓取 GET /api/metrics 的 JSON 即可转换为
  文本暴露格式(docs/operations.md 给出做法),本模块不依赖任何抓取库

命名(与 Prometheus 惯例对齐,便于导出):
- 计数器:http_requests_total / http_status_{code} / llm_calls_total /
  llm_failures_total / llm_retries_total / tool_calls_total /
  tool_failures_total / mcp_calls_total / mcp_call_failures_total /
  circuit_opens_total / lock_conflicts_total / login_failures_total /
  login_throttle_blocks_total / approvals_decided_total
- 计时(秒):http_request_seconds / llm_call_seconds / mcp_call_seconds
- 仪表:llm_slots_in_use / pg_pool_size / pg_pool_in_use
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

GaugeProvider = Callable[[], dict[str, float]]


class Metrics:
    """线程安全的扁平指标集合;一个 web 实例持有一个默认实例(模块级 METRICS)。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._counters: dict[str, int] = {}
        self._timings: dict[str, list[float]] = {}  # name -> [count, total_seconds]
        self._gauges: dict[str, float] = {}         # 进程内维护的简单仪表
        self._providers: dict[str, GaugeProvider] = {}
        self._started = time.monotonic()
        # web.serve() 启动时写入 "host:port";缺省回退 "pid:<pid>"(测试/直构场景)
        self.instance_label = ""

    def inc(self, name: str, amount: int = 1) -> None:
        with self._guard:
            self._counters[name] = self._counters.get(name, 0) + amount

    def observe(self, name: str, seconds: float) -> None:
        """计时埋点:累加 count 与总耗时(sum/count 即均值,无分桶)。"""
        with self._guard:
            slot = self._timings.setdefault(name, [0, 0.0])
            slot[0] += 1
            slot[1] += max(0.0, float(seconds))

    def gauge_add(self, name: str, delta: float) -> None:
        """进程内仪表增减(如 LLM 槽位占用 +1/-1)。"""
        with self._guard:
            self._gauges[name] = self._gauges.get(name, 0.0) + delta

    def register_provider(self, name: str, provider: GaugeProvider) -> None:
        """注册快照期惰性求值的仪表来源(如 psycopg 连接池占用)。"""
        with self._guard:
            self._providers[name] = provider

    def snapshot(self) -> dict[str, Any]:
        with self._guard:
            counters = dict(self._counters)
            timings = {name: {"count": int(count), "sum": round(total, 6)}
                       for name, (count, total) in self._timings.items()}
            gauges = dict(self._gauges)
            providers = tuple(self._providers.values())
        # provider 在锁外求值:慢 provider 不得阻塞埋点写入
        for provider in providers:
            try:
                values = provider()
            except Exception:
                continue  # 观测失败只丢这一路仪表,绝不影响快照与业务
            if isinstance(values, dict):
                gauges.update({key: value for key, value in values.items()
                               if isinstance(value, (int, float))})
        return {
            "uptime_seconds": round(time.monotonic() - self._started, 3),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "counters": counters,
            "gauges": gauges,
            "timings": timings,
        }

    def reset(self) -> None:
        """全部归零(计数/计时/仪表/provider),测试隔离用。"""
        with self._guard:
            self._counters.clear()
            self._timings.clear()
            self._gauges.clear()
            self._providers.clear()
            self._started = time.monotonic()


# 模块级默认实例:web 层与懒导入埋点(llm/engine/mcp_runtime)共用同一进程状态
METRICS = Metrics()


def reset() -> None:
    """清空默认实例(测试隔离)。"""
    METRICS.reset()

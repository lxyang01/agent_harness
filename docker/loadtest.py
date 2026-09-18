#!/usr/bin/env python3
"""宿主机侧轻量压测工具(纯标准库):固定并发对单个只读端点持续打请求,汇总 RPS 与延迟分位。

用法(在仓库根执行,集群需已 `docker compose up`):

    python -X utf8 docker/loadtest.py --path /api/health
    python -X utf8 docker/loadtest.py --path /api/health --concurrency 16
    python -X utf8 docker/loadtest.py --path /api/snapshot --login admin:Smoke-Admin-1 --method POST

注意:
  - **不要对 /api/chat 压测**——该端点触发真实模型调用,按量计费;本工具面向
    只读端点(health / snapshot / bills 查询类)。
  - /api/snapshot 是 POST 端点(每请求真实查询 PG),压测它请加 `--method POST`;
    GET 会被静态文件分支按 404 处理,数字无意义。
  - 单机单进程客户端,worker 线程共享一个 GIL:并发很高时瓶颈可能先出现在
    压测端而非服务端,所得 RPS 应视为下界。
  - 503 是数据不是失败(BoundedHTTPServer 有界线程池满载的预期行为),退出码
    仍为 0;只有全部请求都连不上(集群未启动/端口不通)才以退出码 2 结束。
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import sys
import threading
import time
import urllib.parse
from collections import Counter

DEFAULT_URL = "http://localhost:8080"
DEFAULT_PATH = "/api/health"
REQUEST_TIMEOUT = 10.0  # 单请求超时:防止个别挂死请求拖长压测墙钟


def login(url: str, credentials: str) -> str:
    """登录一次,返回可复用的 Cookie 请求头值(如 "session=<token>")。

    登录接口要求同源:POST 必须携带与 Host 一致的 Origin 头(见 web 层 CSRF 校验)。
    """
    parsed = urllib.parse.urlsplit(url)
    username, sep, password = credentials.partition(":")
    if not sep or not username or not password:
        raise SystemExit("--login 参数格式应为 用户名:密码")
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80,
                                      timeout=REQUEST_TIMEOUT)
    body = json.dumps({"username": username, "password": password})
    headers = {"Content-Type": "application/json",
               "Origin": f"{parsed.scheme}://{parsed.netloc}"}
    try:
        conn.request("POST", "/api/auth/login", body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        if resp.status != 200:
            raise SystemExit(f"登录失败(HTTP {resp.status}),无法开始压测")
        set_cookie = resp.getheader("Set-Cookie")
        if not set_cookie:
            raise SystemExit("登录成功但响应缺少 Set-Cookie,无法复用会话")
    finally:
        conn.close()
    return set_cookie.split(";", 1)[0].strip()  # 只留 name=value


class Stats:
    """线程安全统计:精确状态码计数、延迟样本、连接错误计数。"""

    def __init__(self) -> None:
        self.status_codes: Counter[int] = Counter()
        self.conn_errors = 0
        self.latencies: list[float] = []  # 毫秒;仅统计拿到响应的请求
        self._lock = threading.Lock()

    def record_response(self, status: int, latency_ms: float) -> None:
        with self._lock:
            self.status_codes[status] += 1
            self.latencies.append(latency_ms)

    def record_conn_error(self) -> None:
        with self._lock:
            self.conn_errors += 1

    @property
    def total(self) -> int:
        return sum(self.status_codes.values()) + self.conn_errors

    def percentile(self, p: float) -> float:
        """最近邻法(nearest-rank)取分位:p=95、n=100 → 排序后第 95 个。"""
        if not self.latencies:
            return 0.0
        rank = min(len(self.latencies),
                   max(1, math.ceil(p / 100 * len(self.latencies))))
        return sorted(self.latencies)[rank - 1]


def worker(url: str, path: str, method: str, body: str | None,
           headers: dict[str, str], deadline: float, stats: Stats) -> None:
    parsed = urllib.parse.urlsplit(url)
    while time.perf_counter() < deadline:
        started = time.perf_counter()
        try:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80,
                                              timeout=REQUEST_TIMEOUT)
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                resp.read()  # 必须读完才能复用/关闭连接
                stats.record_response(resp.status,
                                      (time.perf_counter() - started) * 1000)
            finally:
                conn.close()
        except (OSError, http.client.HTTPException):
            stats.record_conn_error()


def run(args: argparse.Namespace) -> int:
    headers: dict[str, str] = {"Connection": "close"}
    if args.login:
        cookie = login(args.url, args.login)
        headers["Cookie"] = cookie
        print(f"已登录({args.login.split(':', 1)[0]}),会话 Cookie 将复用于本次压测")
    body: str | None = None
    if args.method == "POST":
        body = args.body
        headers["Content-Type"] = "application/json"
        # POST 带上与 Host 同源的 Origin,模拟浏览器同源请求(通过 web 层 CSRF 校验)
        parsed = urllib.parse.urlsplit(args.url)
        headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"

    stats = Stats()
    wall_started = time.perf_counter()
    deadline = wall_started + args.duration
    threads = [threading.Thread(target=worker, kwargs={
        "url": args.url, "path": args.path, "method": args.method, "body": body,
        "headers": headers, "deadline": deadline, "stats": stats},
        daemon=True) for _ in range(args.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=args.duration + REQUEST_TIMEOUT + 10)

    wall = time.perf_counter() - wall_started
    total = stats.total
    codes = stats.status_codes
    n_2xx = sum(n for c, n in codes.items() if 200 <= c < 300)
    n_503 = codes[503]
    n_5xx_other = sum(n for c, n in codes.items() if 500 <= c and c != 503)
    n_4xx = sum(n for c, n in codes.items() if 400 <= c < 500)

    print()
    print("=" * 62)
    print(f"压测结果  {args.method} {args.url}{args.path}"
          f"  并发 {args.concurrency}  时长 {args.duration}s")
    print("=" * 62)
    print(f"请求数        {total}(连接错误 {stats.conn_errors})")
    if codes:
        detail = ", ".join(f"{c}: {n}" for c, n in sorted(codes.items()))
        print(f"状态码分布    {detail}")
    print(f"分组统计      2xx={n_2xx}  4xx={n_4xx}  "
          f"503={n_503}  其他5xx={n_5xx_other}")
    print(f"墙钟          {wall:.2f}s")
    print(f"实测 RPS      {total / wall:.0f}" if wall > 0 else "实测 RPS      n/a")
    print(f"延迟 p50      {stats.percentile(50):.1f} ms")
    print(f"延迟 p95      {stats.percentile(95):.1f} ms")
    print(f"延迟 p99      {stats.percentile(99):.1f} ms")

    if total == 0 or stats.conn_errors == total:
        print("全部请求连接失败:集群未启动或地址不可达。")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="BillGuard 只读端点轻量压测(纯标准库,宿主机侧执行)")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"集群入口(默认 {DEFAULT_URL})")
    parser.add_argument("--path", default=DEFAULT_PATH,
                        help=f"压测路径(默认 {DEFAULT_PATH};勿用 /api/chat,计费)")
    parser.add_argument("--duration", type=float, default=10.0,
                        help="压测时长秒数(默认 10)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="并发 worker 线程数(默认 8)")
    parser.add_argument("--login", metavar="USER:PASS", default=None,
                        help="可选;给定则先登录一次并复用会话 Cookie(鉴权端点必填)")
    parser.add_argument("--method", choices=["GET", "POST"], default="GET",
                        help="HTTP 方法(默认 GET;/api/snapshot 等业务端点为 POST)")
    parser.add_argument("--body", default='{"session_id": "default"}',
                        help="POST 请求体 JSON(默认 {\"session_id\": \"default\"})")
    args = parser.parse_args(argv)
    if args.path.startswith("//"):  # Git Bash 下 //api/health 可绕过 MSYS 路径转换
        args.path = args.path[1:]
    if not args.path.startswith("/"):
        raise SystemExit(
            f"路径参数被 shell 改写了(得到 {args.path!r})。Git Bash 会把 "
            "'/api/...' 转成本机路径,请改用 '--path //api/...' 或加前缀 "
            "MSYS_NO_PATHCONV=1;PowerShell/cmd 无此问题。")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())

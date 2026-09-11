#!/usr/bin/env python3
"""生成 BillGuard 演示样例数据(三个月合成账单 + 订阅剧本)。

用法(在项目根执行):

    python scripts/make_sample_data.py [--out sample_data]

固定随机种子(42),输出完全可复现:
  - bills_demo.csv         2026-06-01 ~ 2026-08-31,约 400 笔日常账单
  - subscriptions_demo.csv 6 行订阅表(腾讯视频/百度网盘/网易云音乐/Keep/iCloud/GitHub Copilot)

内置剧本(与 billguard.bills 的异常阈值对齐,保证可检出):
  1. 涨价:腾讯视频 6/7 月实扣 15 元、8 月 25 元,订阅预期 15 元
     (HIKE_MIN_ABS=1、HIKE_RATIO=0.2 → 偏差 10 元必命中 price_hike)。
  2. 重复扣费:百度网盘 2026-08-06 两笔 18 元,间隔 5 分钟
     (DUPLICATE_WINDOW_DAYS=3 → 命中 duplicate)。
  3. 离群:2026-07-15 "某电商平台" 购物 899 元;其余购物笔金额长尾偏小,
     类别均值(含本笔)约 120 元,满足 ≥200 且 ≥5×均值(OUTLIER_MIN/OUTLIER_RATIO)。
     购物金额按 30-140 为主、15% 概率 140-300 抽取,名义区间仍为 30-300。
  4. PII 脱敏演示:两条备注各含一处手机号(138…)与订单号(SO-…),见 mask_pii。

其余固定开支:每月房租 2500 元、水电燃气 80-160 元(居住类)。
"""
from __future__ import annotations

import argparse
import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 42
START_DATE = date(2026, 6, 1)
END_DATE = date(2026, 8, 31)

BILLS_FILENAME = "bills_demo.csv"
SUBS_FILENAME = "subscriptions_demo.csv"
BILLS_HEADER = ("tx_id", "paid_at", "merchant", "category", "amount", "method", "note")
SUBS_HEADER = ("name", "merchant", "cycle", "expected_amount")

# 订阅表:与 subscriptions_demo.csv 完全一致;账单中订阅扣款的商户名必须与之一致,价格比对才能命中
SUBSCRIPTIONS = (
    ("腾讯视频", 15.0, "视频VIP自动续费"),
    ("百度网盘", 18.0, "超级会员自动续费"),
    ("网易云音乐", 10.0, "黑胶VIP自动续费"),
    ("Keep", 19.0, "健身会员自动续费"),
    ("iCloud", 21.0, "50GB云存储订阅"),
    ("GitHub Copilot", 84.0, "编程助手订阅"),
)

MERCHANTS = {
    "餐饮": ("饿了么", "美团外卖", "肯德基", "麦当劳", "瑞幸咖啡", "喜茶"),
    "交通": ("滴滴出行", "地铁通勤", "公交集团", "12306", "中国石化"),
    "购物": ("淘宝", "京东", "拼多多", "天猫超市", "盒马鲜生"),
    "娱乐": ("万达影城", "Steam", "大麦网", "猫眼演出", "任天堂eShop"),
}
DAILY_WEIGHTS = (("餐饮", 45), ("交通", 20), ("购物", 15), ("娱乐", 10))
METHODS = ("微信支付", "支付宝", "银行卡")
NOTES = {
    "餐饮": ("工作日午餐", "下午茶", "加班夜宵", "同事聚餐", "", ""),
    "交通": ("通勤地铁", "打车赶时间", "顺路加油", "", ""),
    "购物": ("日用品补货", "囤纸巾洗衣液", "数码小配件", "", ""),
    "娱乐": ("周末电影", "游戏打折入手", "乐队演出门票", "", ""),
}

# 一行 = (paid_at, merchant, category, amount, method, note)
Row = tuple[datetime, str, str, float, str, str]


def _daily_amount(category: str) -> float:
    if category == "餐饮":
        return round(random.uniform(15, 60), 2)
    if category == "交通":
        return round(random.uniform(8, 45), 2)
    if category == "娱乐":
        return round(random.uniform(20, 120), 2)
    # 购物:名义区间 30-300,长尾偏小额,使类别均值约 100-120,
    # 从而 899 元离群样本稳定满足 ≥5×均值 且 ≥200 的判定
    return round(random.uniform(140, 300) if random.random() < 0.15 else random.uniform(30, 140), 2)


def _daily_rows() -> list[Row]:
    categories = [name for name, _ in DAILY_WEIGHTS]
    weights = [weight for _, weight in DAILY_WEIGHTS]
    rows: list[Row] = []
    day = START_DATE
    while day <= END_DATE:
        for _ in range(random.randint(2, 6)):
            category = random.choices(categories, weights=weights, k=1)[0]
            minute = random.randrange(7 * 60, 22 * 60 + 31)
            stamp = datetime(day.year, day.month, day.day, minute // 60, minute % 60, random.randrange(60))
            rows.append((stamp, random.choice(MERCHANTS[category]), category,
                         _daily_amount(category), random.choice(METHODS), random.choice(NOTES[category])))
        day += timedelta(days=1)
    return rows


def _scripted_rows() -> list[Row]:
    rows: list[Row] = []
    # 每月固定开支:房租 + 水电燃气(居住类)
    for month in (6, 7, 8):
        rows.append((datetime(2026, month, 1, 12, 0, 0), "自如公寓", "居住", 2500.0, "银行卡", f"{month}月房租"))
        rows.append((datetime(2026, month, 2, 10, 30, 0), "国家电网", "居住",
                     round(random.uniform(80, 160), 2), "银行卡", f"{month}月水电燃气"))
    # 订阅扣款:每月 5 日错峰扣费;腾讯视频 8 月起涨到 25 元;百度网盘 8 月重复扣款
    for month in (6, 7, 8):
        for index, (merchant, expected, note) in enumerate(SUBSCRIPTIONS):
            if merchant == "百度网盘" and month == 8:
                rows.append((datetime(2026, 8, 6, 9, 15, 0), merchant, "订阅", 18.0, "支付宝", note))
                rows.append((datetime(2026, 8, 6, 9, 20, 0), merchant, "订阅", 18.0, "支付宝", f"{note}(扣款提醒)"))
                continue
            amount = 25.0 if merchant == "腾讯视频" and month == 8 else expected
            rows.append((datetime(2026, month, 5, 8 + index, 5, 0), merchant, "订阅", amount, "支付宝", note))
    # 剧本 3:购物离群 899 元(备注含订单号,PII 脱敏演示之二)
    rows.append((datetime(2026, 7, 15, 20, 13, 0), "某电商平台", "购物", 899.0, "支付宝",
                 "数码大促下单,订单号 SO-20260715-89901 已开票"))
    # 剧本 4:备注含手机号,供 mask_pii 演示
    rows.append((datetime(2026, 7, 2, 12, 32, 0), "美团外卖", "餐饮", 32.5, "微信支付",
                 "餐品错送退款,联系骑手 13812345678 处理"))
    return rows


def _write_bills(rows: list[Row], path: Path) -> int:
    ordered = sorted(rows, key=lambda row: row[0])
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(BILLS_HEADER)
        for sequence, (stamp, merchant, category, amount, method, note) in enumerate(ordered, start=1):
            writer.writerow((f"TX{sequence:04d}", stamp.strftime("%Y-%m-%d %H:%M:%S"), merchant, category,
                             f"{amount:.2f}", method, note))
    return len(ordered)


def _write_subscriptions(path: Path) -> int:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(SUBS_HEADER)
        for name, expected, _note in SUBSCRIPTIONS:
            writer.writerow((name, name, "月", f"{expected:.1f}"))
    return len(SUBSCRIPTIONS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 BillGuard 三个月演示账单与订阅样例(固定种子,可复现)")
    parser.add_argument("--out", default="sample_data", help="输出目录(默认:sample_data)")
    args = parser.parse_args(argv)
    random.seed(SEED)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    bills_count = _write_bills(_daily_rows() + _scripted_rows(), out_dir / BILLS_FILENAME)
    subs_count = _write_subscriptions(out_dir / SUBS_FILENAME)
    print(f"{out_dir / BILLS_FILENAME} 共 {bills_count} 行({START_DATE} ~ {END_DATE})")
    print(f"{out_dir / SUBS_FILENAME} 共 {subs_count} 行订阅")
    print("剧本:腾讯视频8月涨至25元 / 百度网盘08-06两笔18元间隔5分钟 / "
          "某电商平台07-15购物899元 / 备注含手机号与订单号")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""CI 校验:README 里的测试/对抗探针计数必须与真实值一致(且自身只有一个值)。

README 与代码在迭代中容易脱节(测试加了、README 数字忘了改),本工具把
「数字写对」变成 CI 硬约束。约定写法:

  - 单元测试:`N 项单元测试`(叙述处)与 `Ran N tests`(命令期望注释)
    —— 两类写法提取出的值合并后必须恰好只有一个,且等于 --tests 传入的真实值
  - 对抗探针:`N 条对抗探针`(叙述处)与 `"total": N`(评测期望注释)
    —— 同上,等于 --probes 传入的真实值

CI 用法(数字从当次运行输出中提取,不手写):

    # 单元测试步骤先 tee 输出,再取 Ran N tests 的 N
    grep -oE 'Ran [0-9]+ tests' unittest.log | grep -oE '[0-9]+' | head -1
    # 对抗评测步骤的 tee 产物是 JSON,直接解析 metrics.total
    # (注意不能 grep '"total"':categories 里每个分类也有自己的 total)
    python -c "import json;print(json.load(open('adv-metrics.json'))['metrics']['total'])"
    python tools/check_readme_numbers.py --tests "$tests" --probes "$probes"

本地用法:

    python -X utf8 tools/check_readme_numbers.py --tests 278 --probes 25

任一检查不过:打印中文差异清单并以 1 退出;全部通过打印一行确认并以 0 退出。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"

# 每组的多个写法提取出的值合并成同一个集合,要求恰好一个 distinct 值
UNIT_TEST_PATTERNS = (r"(\d+) 项单元测试", r"Ran (\d+) tests")
ADVERSARIAL_PROBE_PATTERNS = (r"(\d+) 条对抗探针", r'"total": (\d+)')


def collect_values(text: str, patterns: tuple[str, ...]) -> set[str]:
    values: set[str] = set()
    for pattern in patterns:
        values.update(m.group(1) for m in re.finditer(pattern, text))
    return values


def check_group(label: str, unit: str, patterns: tuple[str, ...],
                actual: str, text: str) -> list[str]:
    """返回该组的错误信息列表(空列表 = 通过);unit 用于消息文案(如「项单元测试」)。"""
    values = collect_values(text, patterns)
    if not values:
        return [f"README 中未找到任何{label}计数——约定写法被改写了,请恢复"
                f"(应为「N {unit}」等既有句式),否则 CI 无法校验数字"]
    if len(values) > 1:
        return [f"README 内部不一致:{label}计数出现了多个值 "
                f"{'、'.join(sorted(values, key=int))}——请先统一再同步真实值"]
    (readme_value,) = values
    if readme_value != actual:
        return [f"README 说 {readme_value} {unit},实际 {actual}——请同步 README"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验 README 中的单元测试/对抗探针计数与真实值一致")
    parser.add_argument("--tests", required=True, metavar="N",
                        help="本次 CI 实际跑过的单元测试数(Ran N tests 的 N)")
    parser.add_argument("--probes", required=True, metavar="N",
                        help="本次 CI 实际跑过的对抗探针数(metrics.total 的 N)")
    args = parser.parse_args(argv)

    text = README.read_text(encoding="utf-8")
    errors = [
        *check_group("单元测试", "项单元测试", UNIT_TEST_PATTERNS, args.tests, text),
        *check_group("对抗探针", "条对抗探针", ADVERSARIAL_PROBE_PATTERNS,
                     args.probes, text),
    ]
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    print(f"README 计数与实际一致:{args.tests} 项单元测试、{args.probes} 条对抗探针")
    return 0


if __name__ == "__main__":
    sys.exit(main())

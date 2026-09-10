from __future__ import annotations

import argparse

from .agents import create_planning_agent
from .llm import MockLLM, OpenAICompatibleLLM


def main() -> None:
    parser = argparse.ArgumentParser(description="Framework-free PlanningAgent Harness")
    parser.add_argument("--session", default="default", help="独立项目窗口的 Session ID")
    parser.add_argument("--data-dir", default=".sessions", help="Session、任务与 Trace 存储目录")
    parser.add_argument("--docs-dir", default="docs", help="PlanningAgent 可读取的受限文档目录")
    parser.add_argument("--llm", choices=("mock", "openai"), default="mock")
    parser.add_argument("--model", default="gpt-4.1-mini", help="真实 LLM 模型名")
    parser.add_argument("--base-url", default="https://api.openai.com/v1", help="OpenAI-compatible API 地址")
    args = parser.parse_args()

    llm = MockLLM() if args.llm == "mock" else OpenAICompatibleLLM(args.model, base_url=args.base_url)
    agent = create_planning_agent(llm, args.session, args.data_dir, args.docs_dir)
    print(f"PlanningAgent 已启动（project-session={args.session}），输入 exit 退出。")
    while True:
        try:
            text = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in {"exit", "quit", "退出"}:
            break
        try:
            response = agent.run(args.session, text)
            print(f"Agent: {response.answer}  [steps={response.steps}, trace={response.trace_id[:8]}]")
        except Exception as exc:
            print(f"错误: {exc}")


if __name__ == "__main__":
    main()

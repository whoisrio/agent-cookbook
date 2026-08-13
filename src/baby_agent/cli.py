"""Baby Agent CLI — 底部固定输入框 + 逐字 stream 输出。

用法:
    python -m baby_agent              # 默认模型
    python -m baby_agent --model gpt-4o-mini  # 指定模型
"""

import argparse
from queue import Queue


def run_cli(model_name: str | None = None) -> None:
    """启动 baby agent TUI。"""
    from baby_agent.agent import create_baby_agent
    from baby_agent.app import BabyAgentApp

    # 共享队列：app 往里 put，agent middleware 在对应时机 drain。
    # 不能走 graph state——stream 启动后外部无法回写正在运行的 state。
    steering_queue: Queue = Queue()
    followup_queue: Queue = Queue()

    agent = create_baby_agent(
        model_name,
        steering_queue=steering_queue,
        followup_queue=followup_queue,
    )
    app = BabyAgentApp(
        agent,
        steering_queue=steering_queue,
        followup_queue=followup_queue,
    )
    app.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Baby Agent CLI")
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="模型名称，默认从环境变量 OPENAI_MODEL 读取",
    )
    args = parser.parse_args()
    run_cli(model_name=args.model)


if __name__ == "__main__":
    main()

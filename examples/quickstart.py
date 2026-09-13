#!/usr/bin/env python3
"""最小调用示例：不经过命令行，直接在 Python 里使用。

    python3 examples/quickstart.py

分两步演示：
  1. 只用 DSH 跑一个会话（最低层，不涉及下级 agent）
  2. 完整三层：DSH 委派 Claude Code 干活并独立验收

跑之前建议先执行 `conductor doctor` 确认环境。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

# 让脚本在未安装包时也能直接跑
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from conductor import AgentTranscript, DshClient, DshConfig  # noqa: E402
from conductor.cli import main as conductor_main  # noqa: E402


def step1_talk_to_dsh() -> None:
    """第 1 步：直接和 DSH 说一句话，拿到结构化结果。

    这一层不涉及下级 agent，用来确认 `dsh` 入口与凭据都正常。
    """
    print("=" * 60)
    print("第 1 步：直接驱动 DSH")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="conductor-demo-") as workspace:
        config = DshConfig(workspace=workspace)
        with DshClient(config) as dsh:
            result = dsh.run("Reply with exactly: CONDUCTOR_OK", timeout_ms=120_000)
        print(f"  status     : {result.status}")
        print(f"  final_text : {result.final_text!r}")
        print(f"  turn_end   : {result.turn_end_reason}")
        print(f"  elapsed    : {result.elapsed_ms}ms")


def step2_full_workflow() -> None:
    """第 2 步：完整三层——DSH 委派 Claude Code 并独立验收。

    实际调用 CLI 的 run 子命令，等价于：

        python3 -m conductor run --workspace <tmp> \\
            --task "创建 demo.txt，内容是一行 Hello Conductor" \\
            --verify "demo.txt 存在且内容包含 Hello Conductor"
    """
    print()
    print("=" * 60)
    print("第 2 步：完整工作流（DSH → Claude Code → DSH 验收）")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="conductor-demo-") as workspace:
        code = conductor_main([
            "run",
            "--workspace", workspace,
            "--task", "创建 demo.txt，内容是一行 Hello Conductor",
            "--verify", "demo.txt 存在，且内容包含 Hello Conductor",
            "--agent", "claude",
            "--max-attempts", "2",
        ])
        print(f"\n  退出码: {code}（0 = 验收通过）")

        artifact = os.path.join(workspace, "demo.txt")
        if os.path.exists(artifact):
            with open(artifact, encoding="utf-8") as handle:
                print(f"  产物内容: {handle.read().strip()!r}")

        verdict_path = os.path.join(workspace, ".dsh-orchestrator", "result.json")
        if os.path.exists(verdict_path):
            with open(verdict_path, encoding="utf-8") as handle:
                verdict = json.load(handle)
            print(f"  验收结论: {verdict.get('status')}（{verdict.get('attempts')} 轮）")


def main() -> int:
    step1_talk_to_dsh()
    # 第 2 步会真的调用 Claude Code（消耗额度），所以需要显式确认
    if os.environ.get("CONDUCTOR_DEMO_FULL") != "1":
        print()
        print("（已跳过第 2 步：它会真实调用下级 agent。"
              "要执行请设 CONDUCTOR_DEMO_FULL=1）")
        return 0
    step2_full_workflow()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# 变更记录

## 0.5.3 — 2026-09-24

本版缩短 quickstart 演示任务，并将完整报告的验收部分精简为统一结论。
SDK 接口、JSON 字段、schema 版本及独立验收协议均保持不变。

### 改进

- quickstart 改为创建 `hello.py` 并输出 `Hello, DSH!`，由 DSH 和调用方独立执行确认。
  worker 只需提供简短完整的交接报告，移除订单 CSV、金额处理、多场景单测和额外长报告生成。
- 示例默认开启 `include_report`，直接展示 SDK 返回的报告快照；支持 `--no-include-report`
  关闭，并保留 `--include-report`。SDK 的 `ConductorConfig.include_report` 默认仍为 `False`。
- quickstart 继续保存完整结果 JSON，支持缺少 `report_file` 的内联报告，并展示报告收集或
  落盘警告；错误结果也保留已有报告。报告展示不依赖固定 Markdown 标题。
- 保留 `--tmux-only` 的 6000 行采集自检，默认业务演示不再额外运行该自检；删除示例的
  `--stress-report` 参数。长报告完整性继续由自动化测试覆盖。
- 合并报告保留全部实际 worker 轮次和已登记 Claude Code/Codex 子任务的原文，简化包装信息。
  最后只展示最终状态、简短总结及非空剩余问题，不再逐项展开检查方法、证据和通过情况，
  也不重复列出产物。历史轮次、正文缺失、收集问题和运行错误仍明确标示。
- `verdict.checks`、`verdict.artifacts` 等结构化数据完整保留；DSH 仍须独立验证所有验收项。
  `worker_result`、`dsh.final_text`、事件回调、CLI 单 JSON 输出及退出码保持原有语义。

### 使用

```bash
python3.13 -m pip install --upgrade dsh-conductor==0.5.3

# 在源码仓库运行：默认打印 worker 报告和统一验收结论
python3.13 examples/quickstart.py
python3.13 examples/quickstart.py --agent codex
python3.13 examples/quickstart.py --no-include-report
python3.13 examples/quickstart.py --tmux-only
```

已有 SDK 调用无需迁移。`report` 仍是 Markdown 字符串，`report_file` 和 `report_warnings`
的类型与可选规则不变；需要逐项审计详情时继续读取 `result.verdict.checks` 或 JSON `verdict`。

### 验证

- 135 项自动化测试全部通过，包含协议模拟、真实 tmux、报告完整性和 SDK/CLI 兼容检查。
- 编译检查、独立 tmux 自检、wheel/sdist 构建、元数据及 wheel 内容检查通过；隔离安装后的
  SDK 和内置 skill 验证通过，两种 agent 均完成协议模拟验证。
- 本次本地环境未提供 `dsh` 命令，未进行真实模型端到端运行，也未测量模型执行耗时。

## 0.5.2 — 2026-09-21

本版新增可选完整报告返回：将**所有实际 worker 轮次的报告、已登记 Claude/Codex 内部子任务的
完整报告正文，以及任务验收报告**合并返回。解决此前最终结果侧重验收、worker_result 只有最后
一轮文件路径、调用方需要自己查文件才能获得业务回答的问题。

### 新增与改进

- SDK 新增 `ConductorConfig(include_report=True)`；CLI 新增 `--include-report`。默认关闭，
  不改变旧调用的输出字段和任务执行方式。
- 开启后 `TaskResult.report` / JSON `report` 直接包含完整 Markdown 正文；`report_file` 指向
  本轮运行目录下原子写入的 `report.md`；`report_warnings` 列出收集或持久化问题。
- 第一部分是“所有任务结果报告”：按执行顺序收集各轮 result.md，并展开该轮清单中所有层级的
  子任务报告。历史返工轮次有明确标记，不混入未使用的候选 agent 和预建轮次目录。
- 第二部分是“任务验收报告”：由 SDK 从已校验 verdict 生成，包含最终状态、总结、每项验收要求、
  检查方法、证据、通过情况、产物与剩余问题，确保文字报告与结构化结论一致。
- 报告正文直接读取文件，不依赖模型再次复述，不从 tmux 屏幕拼接，不按行数截断；覆盖长中文文本、
  Unicode 和 6000 行报告的完整性验证。普通检查日志可继续作为附件，不自动展开任意引用文件。
- 完整报告在返回前形成固定快照；后续文件修改或删除不会改变 `to_json()` 返回的正文。
- 新增每轮 `subtask-reports.json` 清单，绑定 receipt token，记录子任务 id、parent_id、agent、
  title、status 和 report_file；没有内部子任务也要求保存空清单。两种 worker 的交接指令和
  中文 skill 文档同步更新，要求先保存完整子报告和清单，再提交 receipt。
- 清单检查拒绝越界路径、越界链接、非普通文件、重复项和不匹配的 token；损坏、缺失和未完成
  内容明确列出，避免把不完整收集当作完整报告。
- rejected 仍返回合法 TaskResult 并保留已有正文。超时/取消/协议错误继续抛 ConductorError；
  核心清理后在剩余预算内尽力提供 `exc.report`，没有可信 verdict 时明确说明未形成有效验收结论。
  报告处理不延长总时限，不覆盖首次执行错误。report.md 写入失败时保留内联正文并给出提示。
- quickstart 新增 `--include-report`，会打印合并正文并保存到 quickstart-result.json；可与
  `--stress-report` 一起使用。`conductor show` 新增展示已落盘的 report_file 路径。
- 发布流程支持推送版本标签后自动测试、构建、Trusted Publishing 上传 PyPI，并从本 CHANGELOG
  提取完整版本说明创建 GitHub Release。保留原有 Release 与手动 workflow 发布入口。

### 升级与使用方法

```bash
python3.13 -m pip install --upgrade dsh-conductor==0.5.2
```

Python SDK：

```python
from conductor import Conductor, ConductorConfig, ConductorError

client = Conductor("/path/to/workspace", ConductorConfig(include_report=True))
try:
    result = client.run("请使用 Codex 完成任务。验收标准：所有测试通过。")
except ConductorError as exc:
    print(exc.code, str(exc))
    if exc.report is not None:
        print(exc.report)  # 可能是部分报告，包含收集未完成说明
else:
    print(result.report)          # 完整业务报告正文 + 逐项验收报告
    print(result.report_file)     # report.md 的 Path；落盘失败时为 None
    print(result.report_warnings) # 没有收集问题时为空 tuple
    payload = result.to_json()    # payload["report"] 同样包含完整正文
    print(payload["status"])     # 业务成功仍看 accepted/rejected
```

CLI（stdout 仍只有一个 JSON，实时进度写 stderr）：

```bash
python3.13 -m conductor run \
  --workspace /path/to/workspace \
  --prompt '请使用 Codex 完成任务。验收标准：所有测试通过。' \
  --include-report > result.json

python3.13 -c 'import json; print(json.load(open("result.json"))["report"])'

# 在源码仓库运行完整示例
python3.13 examples/quickstart.py --agent codex --include-report
python3.13 examples/quickstart.py --agent claude --include-report --stress-report
```

### 兼容性与边界

- `include_report=False` 是默认值；旧代码无需迁移，旧 JSON 不新增报告字段。
- `worker_result` 仍是最后一轮 result.md 路径，`dsh.final_text` 仍是 DSH 原始最后回复，
  `status`、`accepted`、`verdict` 的验收含义不变。显示完整结果请读取新增 `report`。
- run() 调用签名、事件回调、CLI 单 JSON 协议、退出码及顶层/plan/verdict/receipt schema 版本不变。
  SDK 仍要求 Python 3.13，仅使用标准库运行时依赖。
- 本次 run 仍只选择一个 Claude 或 Codex worker；内部子任务清单不引入多 worker 调度或中途切换。
- `report_warnings` 表示报告收集问题，不自动推翻独立验收；原有 accepted 对合法回执和非空
  result.md 的校验继续执行。无警告也不意味着 SDK 独立发现了所有内部委派。
- 完整性覆盖已保存、已登记的报告；未登记或从未落盘的历史子任务正文无法自动恢复。
  未生成的 report_file、空 report_warnings 在 JSON 中省略。正文体积随报告总量增长。

完整清单格式、失败处理和集成细节见
[完整报告使用指南](https://github.com/66neko/dsh-conductor/blob/v0.5.2/docs/full-report.md)。

## 0.5.1 — 2026-09-20

- 修复 quickstart 在 tmux 3.2a 下因捕获文本的行尾填充空格误报“全量历史缺行或顺序错误”并提前退出。
- 自检比较忽略行尾空白，继续严格校验历史行数、内容、顺序和日志完整性。
- 新增回归测试，覆盖行尾填充兼容以及漏行、重复、乱序和额外空行的拒绝检查。

## 0.5.0 — 2026-09-19

- 新增 cancel_event、整个 run 的截止时间、独立清理预算和 workspace 重入保护。
- DSH 使用可取消的非阻塞管道，严格要求 turn/end 与 idle 同时完成，拒绝非法协议帧。
- 每轮使用私有 tmux socket、资源身份登记、CleanupReport 和幂等 cleanup_run/CLI cleanup。
- ConductorError 新增 code/phase/details/cleanup；清理失败时保留可信 exc.result。
- CLI 将 SIGINT/SIGTERM 转为取消并输出单个 JSON，超时退出码 124，信号取消为 130/143。
- 进度队列有界，回调与日志收尾遵循剩余预算。新增生命周期故障与真实 tmux 隔离测试。
- 恢复清理拒绝损坏的进程身份记录；日志写锁和 DSH 关闭阶段遵守各自截止时间。
- 兼容性变化及 Linux 资源核验边界见 [迁移说明](docs/lifecycle.md#从-041-迁移)。

# 发布到 PyPI

仓库已经包含完整的 GitHub Actions 发布链路。`.github/workflows/publish.yml` 分为两个
job：`build` 只负责测试和构建，`publish` 只负责使用 GitHub OIDC 向 PyPI 上传已经验证的
wheel 与 sdist。发布不需要在 GitHub Secrets 中保存 PyPI API token。

## 一次性配置

1. 在 GitHub 仓库 `Settings -> Environments` 创建环境 `pypi`。可以为这个环境设置必需的
   审批人，作为生产发布的人工闸门。
2. 登录 PyPI，打开账户的 `Publishing` 页面，添加 GitHub Actions Trusted Publisher。
   首次发布项目尚不存在时，使用 `pending publisher`；项目创建后可在项目设置中管理同一
   publisher。
3. 填写以下值：

   | PyPI 字段 | 值 |
   |---|---|
   | Owner | `66neko` |
   | Repository name | `dsh-conductor` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

   PyPI 项目名是 `dsh-conductor`，仓库地址为
   `https://github.com/66neko/dsh-conductor`。Trusted Publisher 的 workflow 文件名必须
   与 `.github/workflows/publish.yml` 完全一致。

## 发布流程

项目版本唯一由 `conductor/_version.py` 中的 `__version__` 定义。创建与该版本匹配的 Git
tag 并推送（从 0.5.2 起推荐）：

```bash
VERSION=$(python3.13 -c 'from conductor._version import __version__; print(__version__)')
git tag "v$VERSION"
git push origin "v$VERSION"
```

推送标签会自动触发 `publish.yml`：检查 tag 严格等于 `v<project version>`，先运行测试、
编译检查、构建和包内容检查，再将相同发行包交给 Trusted Publishing job 上传 PyPI。
上传成功后独立 release job 从 CHANGELOG.md 提取该版本的完整说明，并创建 GitHub Release。
只有这个 job 具有 contents:write；PyPI job 仍只使用 OIDC。自动 Release 使用 GITHUB_TOKEN
创建，不会再次触发 release 事件工作流，避免重复上传。发布前必须先在 CHANGELOG.md 写好版本章节。

原有手动创建 Release 的入口仍保留；对于已经通过标签自动发布的版本，不要再重复触发发布。

也可以在 Actions 页面手动运行 `Publish to PyPI`，填写已经推送的版本标签，例如
`v<project-version>`。手动运行同一版本会被 PyPI 拒绝，这是预期行为；每个版本只能发布一次。工作流
不会接受分支名或普通提交作为发布来源。

## 发布前本地检查

```bash
python3.13 -m unittest discover -v
python3.13 -m compileall -q conductor skills tests
python3.13 -m pip install build twine check-wheel-contents
python3.13 -m build --sdist --wheel
python3.13 -m twine check dist/*
python3.13 -m check_wheel_contents dist/*.whl
```

仓库代码可以准备 workflow 和元数据，但不能代替 PyPI 账户创建 Trusted Publisher 或
GitHub 环境审批规则；这两项必须由项目维护者在对应网站完成。

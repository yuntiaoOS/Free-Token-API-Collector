# Free Token API Collector

自动从 GitHub、论坛等公开渠道采集免费 AI API（OpenAI / Claude 等），验证可用性后写入 [cc-switch](https://github.com/farion1231/cc-switch) 数据库，供 Claude Code 等工具直接使用。

**流程：** 采集 → 真实问答验证 → 写入 cc-switch → 定期复检 / 清理失效 Key

---

## 环境要求

- **Python** 3.10 及以上
- 已安装并使用过 **cc-switch**（程序会自动检测 `~/.cc-switch/cc-switch.db`）
- 可选：HTTP 代理（访问 GitHub / 部分论坛时需要）

---

## 快速开始

### 方式一：一键运行（推荐）

| 平台 | 操作 |
|------|------|
| Windows | 双击 `run.bat` |
| PowerShell | `.\run.ps1` |

脚本会自动：

1. 检查 Python 环境
2. 安装 `requirements.txt` 依赖
3. **清除** cc-switch 中失效的 API Key（`--purge`）
4. **采集**并验证可用 Key，写入 cc-switch（`main.py`）
5. 将日志保存到 `logs/run_时间戳.log`

> 不带参数时执行「清除 + 采集」两步；传参时仅执行对应命令，例如 `run.bat --list`、`run.bat --validate`。

> 设置环境变量 `FREE_TOKEN_NONINTERACTIVE=1` 可跳过结束时的「按 Enter 退出」，适合自动化任务。

### 方式二：命令行

```bash
pip install -r requirements.txt
python main.py
```

---

## 命令说明

```bash
python main.py                  # 一次性采集 + 验证 + 写入 cc-switch（默认）
python main.py --daemon         # 守护模式，定时采集与复检
python main.py --validate       # 仅重新验证已有 auto provider
python main.py --clean          # 将连续失败次数过多的 provider 标记为 expired
python main.py --purge          # 遍历 cc-switch，验证并删除失效 API Key
python main.py --purge --dry-run  # 预览将删除的条目，不改动数据库
python main.py --list           # 列出当前自动发现的 provider
python main.py --config 路径     # 指定配置文件（默认 config.yaml）
```

一键脚本同样支持传参，例如：

```bat
run.bat --list
run.bat --validate
run.bat --purge --dry-run
run.bat --purge
```

### 验证与清理

写入 cc-switch 前，程序会发送**真实问答**（非仅连通性探测），确认 API 能正常回复后才视为可用。

| 命令 | 作用 |
|------|------|
| `--validate` | 复检已有 `auto_discovered` provider，更新健康状态与配置 |
| `--clean` | 按 `max_consecutive_failures` 将多次失败的 provider **标记**为 `expired`（不删除） |
| `--purge` | 遍历 cc-switch：删除已标记 `expired` 的条目，并对剩余 auto provider 实测后**物理删除**失效 Key |
| `--purge --dry-run` | 仅预览将删除的 provider，不修改数据库 |

建议清理流程：先 `--purge --dry-run` 确认 → 再 `--purge` 正式清除。

### 守护模式

```bash
python main.py --daemon
```

按 `config.yaml` 中 `scheduler` 配置定时执行：

- 定期重新采集并写入
- 定期复检已有 provider
- 自动将连续失败的 provider 标记为 `expired`（`cleanup_expired`）

如需彻底删除失效 Key，可定时执行 `python main.py --purge`（建议配合 `--dry-run` 先预览）。

---

## 手动导入 Provider

适用于你已知的 API 地址和 Key，跳过采集直接验证并写入。

### 先测试、再导入

导入前建议先用 `--test-only` 验证能否正常回答，确认无误后再正式写入：

```bash
# 仅测试，不写入 cc-switch（输出含 sample_reply）
python main.py --import --test-only \
  --base-url https://example.com/v1 \
  --api-key sk-xxxx

# 测试通过后再正式导入
python main.py --import \
  --base-url https://example.com/v1 \
  --api-key sk-xxxx \
  --source manual:my-provider
```

也可使用独立脚本：

```bash
python import_provider.py --test-only --base-url https://example.com/v1 --api-key sk-xxxx
python import_provider.py --base-url https://example.com/v1 --api-key sk-xxxx
```

### 单条导入

```bash
python main.py --import \
  --base-url https://example.com/v1 \
  --api-key sk-xxxx \
  --source manual:my-provider \
  --model gpt-4o-mini
```

| 参数 | 说明 |
|------|------|
| `--base-url` | Provider 的 API 根地址（需含 `/v1`） |
| `--api-key` | API Key |
| `--source` | 来源标签，默认 `manual:import` |
| `--model` | 指定测试模型，可多次传入 |
| `--skip-discover-models` | 跳过 `GET /models` 自动发现 |
| `--test-only` | 仅验证并输出 `sample_reply`，不写入 cc-switch |

### 批量导入

复制示例文件并填入真实 Key：

```bash
copy providers.example.yaml providers.yaml
```

编辑 `providers.yaml` 后执行：

```bash
python main.py --import-file providers.yaml
```

`providers.yaml` 格式示例：

```yaml
providers:
  - source: "manual:xiaomi-mimo"
    base_url: "https://token-plan-cn.xiaomimimo.com/v1"
    api_key: "你的真实 Key"
    models:
      - "mimo-v2.5"

  - source: "manual:another"
    base_url: "https://example.com/v1"
    api_key: "另一个 Key"
    skip_discover_models: true
    models:
      - "gpt-4o-mini"
```

批量导入时加 `--continue-on-error` 可在单条失败时继续处理后续条目；加 `--test-only` 可批量测试而不写入。

```bash
python main.py --import-file providers.yaml --test-only
python import_providers.py --file providers.yaml --test-only
```

> `providers.yaml` 含敏感信息，已加入 `.gitignore`，请勿提交到仓库。

---

## 配置说明

主配置文件为 `config.yaml`，常用项如下。

### 网络

```yaml
network:
  proxy: "http://127.0.0.1:7897"   # 留空或删除则不使用代理
  timeout_seconds: 15
```

### GitHub

```yaml
github:
  token: ""              # 可选，填写可提高 API 限额、减少限流
  search_max_repos: 10
  request_delay_seconds: 2
```

### cc-switch

```yaml
ccswitch:
  db_path: ""                        # 留空则自动检测当前用户目录
  auto_category: "auto_discovered"   # 写入的分类名
  never_overwrite: true              # 不覆盖用户手动添加的 provider
```

自动检测路径（按优先级）：

- `config.yaml` 中配置的 `db_path`
- `~/.cc-switch/cc-switch.db`
- `%USERPROFILE%\.cc-switch\cc-switch.db`（Windows）

### 数据源

在 `sources` 中启用或调整采集来源：

| type | 说明 |
|------|------|
| `github_readme` | 从指定 GitHub 仓库 README 提取 URL + Key |
| `github_search` | 通过 GitHub Search 发现相关仓库并解析 README |
| `web_aggregator` | 从配置的网页聚合站抓取 |
| `forum` | 从 V2EX、Linux.do、NodeSeek 等论坛帖子提取 |

论坛类站点若需登录，在对应 `sites` 条目下配置 `cookie`：

```yaml
- name: linux.do
  platform: discourse
  cookie: "你的 Cookie 字符串"
  entry_urls:
    - https://linux.do/c/welfare/36
    - https://linux.do/tags/c/welfare/36/183-tag/183
    - https://linux.do/tag/mimo/1562/l/latest
    - https://linux.do/search?q=free%20api
    - https://linux.do/search?q=chatgpt%20api
    - https://linux.do/c/ai/93
```

### 调度与验证

```yaml
scheduler:
  refresh_interval_hours: 4      # 守护模式：重新采集间隔
  validate_interval_hours: 1     # 守护模式：复检间隔
  cleanup_expired: true          # 守护模式：自动标记连续失败的 provider
  max_consecutive_failures: 3    # 连续失败超过此次数则标记为 expired（--clean）

validator:
  max_concurrent: 10
  request_timeout_seconds: 10
  discover_models: true
  test_model_openai: "gpt-4o"
  test_model_anthropic: "claude-sonnet-4-20250514"
  # 写入 cc-switch 前发送真实问答，确认能正常回复
  test_prompt: "请用一句话回答：1+1等于几？只回复数字或简短答案。"
  test_max_tokens: 32
  min_reply_chars: 1             # 回复内容最少字符数，低于此值视为失败
  prefer_codex: true             # 优先探测 Codex /responses，可用则写入 codex
```

验证逻辑说明：

- 默认**优先探测 Codex**（`/responses`），通过严格校验的 API 写入 `codex` 分类
- 不支持 Codex 时依次回退 OpenAI Chat（`openclaw`）、Anthropic（`claude`）
- Codex 判定需符合真实 Responses API 结构（`object=response` 等），避免误判
- 成功时记录 `sample_reply`，日志中可查看回复预览
- 空回复、鉴权失败、限流等均判为不可用，不会写入 cc-switch

可将 `prefer_codex: false` 改为优先 Chat 模式（适合仅需 openclaw 的场景）。

---

## 日志

- 一键脚本日志：`logs/run_YYYYMMDD_HHMMSS.log`
- 程序运行日志：由 `logger.py` 配置，同样输出到 `logs/` 目录

`logs/` 目录已在 `.gitignore` 中忽略。

---

## 项目结构

```
free-token-api/
├── main.py              # 主入口
├── config.yaml          # 配置文件
├── run.bat              # Windows 一键运行
├── run.ps1              # PowerShell 一键运行
├── validator.py         # Token 可用性验证
├── db_writer.py         # 写入 cc-switch 数据库
├── import_provider.py   # 单条导入逻辑
├── import_providers.py  # 批量导入逻辑
├── scheduler.py         # 守护模式调度
├── providers.example.yaml
├── requirements.txt
└── sources/             # 各采集源实现
    ├── github_readme.py
    ├── github_search.py
    ├── web_aggregator.py
    └── forum.py
```

---

## 常见问题

**Q: 提示找不到 cc-switch 数据库？**

确保已安装 cc-switch 并至少运行过一次，或手动在 `config.yaml` 中设置 `ccswitch.db_path`。

**Q: GitHub 采集失败或很慢？**

配置 `network.proxy`，并视情况填写 `github.token`（GitHub Personal Access Token，无需特殊权限）。

**Q: 论坛源抓不到内容？**

部分站点需要登录 Cookie；在 `config.yaml` 对应站点下填写 `cookie`，并确认代理可访问该站点。

**Q: 写入 cc-switch 后看不到？**

检查 cc-switch 中 `auto_discovered` 分类；使用 `python main.py --list` 确认是否已写入。

**Q: 如何清理失效的 API Key？**

```bash
python main.py --purge --dry-run   # 先预览
python main.py --purge             # 确认后删除
```

`--purge` 会物理删除验证失败及已标记 `expired` 的 provider，不会删除手动添加的条目（`never_overwrite: true` 时）。

**Q: `--clean` 和 `--purge` 有什么区别？**

- `--clean`：仅将连续失败次数达标的 provider **标记**为 `expired`，数据仍保留在数据库
- `--purge`：发送真实问答验证后，**删除**失效 Key；同时清除已标记 `expired` 的条目

---

## 免责声明

本工具仅从**公开渠道**采集信息，用于个人学习与测试。请遵守各平台服务条款，勿将采集到的 Key 用于商业或滥用场景。API Key 的合法性与稳定性由第三方提供方决定，本工具不保证长期可用。

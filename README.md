# Jev RPA 客服助手

**把电商聊天页面、视觉识字和固定话术选择串成可检查、可追溯的桌面工作流。**

这是一个基于 Python、Clicknium、视觉模型与 Jev Choice 的 Windows 客服原型。
它从聊天界面发现消息，识别客户问题，在经过审核的话术库中逐层选择回复，并在执行前后核对页面状态。
重点是让非技术商家能够配置、预览、验收并追溯处理过程。

当前源码版本为 **0.3.5**。闲鱼网页版＋Edge 是已有适配代码和历史实机检查依据的场景；1688 是后续目标，**不代表已经完成 1688 接待界面的适配或稳定性验收**。
此仓库发布源码、测试与文字文档；没有截图、照片、演示视频或真实商家数据。

## 先了解项目

| 你关心什么 | 从这里开始 |
|---|---|
| 项目要解决什么问题 | [产品设计与能力边界](docs/PRODUCT.md) |
| 程序如何运行、各模块负责什么 | [架构与调用链](docs/ARCHITECTURE.md) |
| 先跑一个不调用 API 的演示 | [快速开始](#快速开始) |
| 第一次连接模型和录制页面 | [配置与使用](docs/SETUP.md) |
| 如何准备统一回复 | [话术库格式与审核](docs/KNOWLEDGE.md) |
| 日志、数据库和费用如何留存 | [数据与可追溯性](docs/DATA_AND_COSTS.md) |
| 测试覆盖了什么、尚未证明什么 | [验证记录](docs/VERIFICATION.md) |
| 找不到控件、不能发送或结果未知 | [故障排查](docs/TROUBLESHOOTING.md) |

## 核心工作方式

```text
左侧未读会话 ──┐
              ├─ 单线程任务队列 ─ 页面快照 ─ 视觉转录 ─ Jev 分层选择
当前会话变化 ──┘                                      │
                                  ┌───────────────────┴──────────────────┐
                                人工处理                              固定回复候选
                                  │                                     │
                              记录原因                  身份／最新位置／草稿／变化／重复核对
                                                                        │
                                                           粘贴、读回、点击与结果核对
                                                                        │
                                                          本地日志、决策、截图和费用
```

- **双入口发现消息：**未读会话与当前会话内容变化交替优先，处理共用串行流程；不创建两条并行发送链。
- **模型分工：**视觉模型转录可见聊天；Jev 逐层选类别、固定话术并复核。回复正文来自话术库，不由模型自由生成。
- **执行前后核对：**检查客户、页面最新位置、人工草稿和消息变化；闲鱼适配通过网页新增卖家气泡核对本次回复。
- **人工接管：**无法回答、定位不确定或发送未知时保留原因；客户暂停与全局停止分开处理。
- **本地留证：**使用 SQLite 保存操作链、截图、模型返回、决策、API 用量与估算费用。
- **首次使用向导：**服务接入、平台录制、位置验证、话术审核、接待验收与完成；打开界面不自动开始接待。

## 能力与验证边界

| 能力 | 本仓库提供 | 仍需验证 |
|---|---|---|
| 分层固定话术选择 | Choice 请求、结果解析、逐层路由、最终复核 | 真实业务问题集上的选话术效果 |
| 当前会话追问监测 | 内容摘要、两轮稳定确认、与未读入口去重 | 各商家页面及长时间运行情况 |
| 闲鱼＋Edge 页面操作 | 定位器接入、未读规则整理、读文字、滚动和发送核对 | 当前网页版本与本机环境 |
| Excel／CSV 话术库 | 检查、预览、审核、版本启用与恢复 | 商家回复内容及适用条件 |
| 发送保护 | 草稿、身份、消息变化、限速、去重、结果未知暂停 | 用户控制的真实测试会话 |
| 1688及其他平台 | 可复用的适配接口与录制流程 | 具体页面、选择器、消息结构和发送方式 |

离线测试通过不等于真实接待稳定；网页出现回复气泡也不是平台服务器送达回执。

## 快速开始

已在 Windows／Python 3.13 环境执行发布验证，依赖版本见 [requirements-windows.txt](requirements-windows.txt)。

### 1. 离线演示

在源码根目录使用 PowerShell：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:JEVTOOL_DATA_DIR = Join-Path $PWD '.local-data'
.\.venv\Scripts\python.exe app.py --demo
```

演示使用 `MOCK-NOT-JEV` 模拟返回，只说明调用链能接通；不调用模型、不读取聊天页、不发送，也不衡量真实准确率。

### 2. Windows 桌面界面

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\.venv\Scripts\python.exe app.py
```

按向导配置自己的模型账号和 Clicknium。此阶段的真实 API 测试可能计费；只有用户明确执行发送验收或启用接待后，才进入相应业务流程。
详细要求见 [配置与使用](docs/SETUP.md) 和 [首次使用说明](FIRST_RUN.md)。

### 3. 离线测试

```powershell
$env:JEVTOOL_DATA_DIR = Join-Path $PWD '.local-data'
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用隔离目录、模拟聊天与模拟服务；不以真实发送替代测试。最新本地执行结果见 [验证记录](docs/VERIFICATION.md)。

## 技术栈

| 层 | 选型 | 负责什么 |
|---|---|---|
| 桌面界面 | PySide6／Qt Widgets | 向导、接待工作台、历史、费用与设置 |
| 调度与业务 | Python、串行 Worker、Service | 停止、状态衔接与完整处理流程 |
| 界面自动化 | Clicknium | 元素定位、网页读取、脚本、点击与快捷键 |
| 视觉转录 | MiniMax 或受支持的兼容／Dify 接口 | 将聊天截图转为结构化消息 |
| 话术决策 | Jev Choice | 从当前目录选类别、话术与人工选项 |
| 存储与导入 | SQLite、CSV、openpyxl | 审计、去重、费用、Excel 与版本记录 |

## 目录

```text
app.py                    程序入口与诊断参数
jevtool/                  界面、调度、模型、自动化与本地存储
knowledge/                示例话术与分类说明
tests/                    离线测试与模拟聊天
docs/                     产品、架构、使用与验证说明
reference/                视觉提示词与来源说明
.locator/README.md        五个录制目标说明，不包含商家定位器
assets/icons/             界面所需的文本 SVG 图标，无照片或截图
packaging/                PyInstaller 与 Inno Setup 构建定义
.github/                  自动检查与问题反馈模板
```

## 数据与第三方服务

默认用户目录为 `%LOCALAPPDATA%\JevReplyTool`，可用 `JEVTOOL_DATA_DIR` 指定隔离目录。
配置中的 API Key、真实定位器、运行数据库、日志、截图和诊断包不要提交到 GitHub。
上传聊天截图会把客户内容交给配置的模型服务，需在程序中确认授权。

API 费用取决于供应商实际用量与价格；本地金额是记录的用量和单价得出的估算，不是供应商账单。
Clicknium 的使用、运行机器数与商业分发另受其授权约束，仓库的 MIT 许可不替代第三方授权。

## 项目来源与许可

本项目参考并演进自 [tmwgsicp/dify-on-qianniu](https://github.com/tmwgsicp/dify-on-qianniu)，保留上游 MIT 版权说明。
在原有 RPA 思路上加入固定话术分层选择、视觉转录边界、双入口串行监测、桌面配置向导与本地审计。
开发过程中使用 AI 辅助编码与迭代，设计判断和验证边界以仓库代码、测试和文档为准。

应用代码采用 [MIT License](LICENSE)，依赖许可见 [第三方说明](THIRD_PARTY_NOTICES.txt)。
欢迎提交可复现的问题与改进建议，见 [贡献指南](CONTRIBUTING.md)；不要上传真实客户聊天或密钥。

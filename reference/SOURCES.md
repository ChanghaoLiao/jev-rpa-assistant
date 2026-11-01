# 来源与实现依据

## 上游

- 仓库：https://github.com/tmwgsicp/dify-on-qianniu
- 参考提交：`8e413a5a9157b0452f8a128a4e94a59b2625369d`
- 上游采用 MIT 许可；版权声明保留在根目录 LICENSE。
- 原思路是利用 Clicknium 操作聊天控件，再调用模型处理，并非 1688 官方消息接口。

本仓库在该思路上演进为分层固定话术选择、视觉转录、可验收的桌面产品和本地审计。
不附带上游商家定位器，也不把上游页面适配视为本项目当前环境已验收。

## 服务与框架文档

- Jev API：https://docs.typesafe.ai/api
- Jev Choice：https://docs.typesafe.ai/primitives/choice
- Jev 置信度：https://docs.typesafe.ai/confidence
- Clicknium：https://clicknium.com/documents/
- Qt for Python：https://doc.qt.io/qtforpython-6/
- openpyxl：https://openpyxl.readthedocs.io/

本项目使用 HTTP Choice 结构和当前层选项，不把模型分数当作业务正确率。
旧配置中的数值门槛保留兼容，当前路由代码不再据此额外拦截明确选择。
外部服务文档、价格与模型可用性可能变化，配置后必须分别测试。

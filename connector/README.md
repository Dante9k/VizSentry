# 本地飞书侧边栏连接器

构建后由 VizSentry 的 `/connector/` 提供。先在主应用创建配对，打开目标多维表格，
通过飞书侧边栏扩展的开发/自定义页面入口加载本地连接器地址，粘贴配对码。
飞书租户策略必须允许本地扩展页面；若当前环境不提供该入口，应改用应用凭证模式。
不要把配对 URL 或配对码发布到公开地址。

本实现依赖官方 `@lark-base-open/js-sdk` 1.0.2，只使用读取方法：
`getActiveTable`、`getFieldMetaList`、`getRecordsByPage`（每页 200）、
`getAttachmentUrls`。临时附件链接在读取当前行时立即下载并上传到本机。
单个附件失败保留原行和错误；分页、行数或记录标识失败则不会宣布导入完成。

原始记录按每批 200 行写入本机 SQLite；配对令牌随机生成、有效期 24 小时。
完成后的会话和附件可供任务重启后恢复，不需要保留 SDK 临时链接。
所有外部桥接请求必须携带 `X-Connector-Token`。本机 API 的来源校验和连接器
能力令牌校验由后端同时执行。连接器不修改飞书数据。

官方说明：<https://lark-base-team.github.io/js-sdk-docs/zh/start/env>
及 <https://lark-base-team.github.io/js-sdk-docs/zh/api/table>。
当前没有真实测试表；实际租户侧边栏加载、附件跨域权限及登录流程仍需验收。

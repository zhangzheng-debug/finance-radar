# Finance Radar 浅色 UI 接口预览

这份预览把浅色研究工作台和证据分流动画接到了 Finance Radar 现有只读接口。

## 已连接的数据

- `/api/v1/overview`：事件总量、核验/排除数量、优先队列、采集更新时间
- `/api/v1/events`：今日新增、事件列表、搜索和状态筛选
- `/api/v1/events/{event_id}`：当前选择事件的证据摘要和来源信息

页面不会调用写接口，也不包含下单、持仓、余额或交易执行功能。

## 本地启动

在项目根目录打开 PowerShell，先启动现有 API：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

再打开第二个 PowerShell 窗口，启动浅色 UI：

```powershell
.\.venv\Scripts\python.exe scripts\serve_light_ui_preview.py --port 8502
```

浏览器打开：

`http://127.0.0.1:8502/`

不要直接双击 HTML。直接使用 `file://` 打开时没有同源代理，页面无法读取本地 API。

## 设计说明

预览服务器只把浏览器的 `GET /api/v1/*` 请求转发到本机 API，其他 HTTP 方法统一返回 `405 READ_ONLY_PREVIEW`。API 目标默认限制在 `localhost` / `127.0.0.1` / `::1`，因此不需要扩大生产 API 的 CORS 白名单。

如果 API 暂时不可用，页面会明确显示“接口暂不可用”并保留静态示例；如果 API 已连接但数据库为空，则显示 0 指标、空事件流和“暂无事件详情”。

# 目录结构

> 本项目后端的代码组织方式。

---

## 概述

项目是一个扁平的双模块 Python 包，包含一个静态前端。无 `src/` 目录——所有 Python 文件都位于仓库根目录，与前端并列。

---

## 目录布局

```
sms-forwarding/
├── main.py             # FastAPI 应用入口
├── at_serial.py        # AT 串口通信层
├── static/
│   └── index.html      # 单页前端仪表板
├── pyproject.toml      # 项目元数据和依赖
└── .trellis/           # Trellis 工作流和规范
```

---

## 模块职责

| 文件 | 职责 |
|------|------|
| `at_serial.py` | 底层异步串口 AT 命令传输。负责 `ATSerial` 类、串口生命周期、基于队列的读取循环、命令/响应匹配、以及短信发送协议。 |
| `main.py` | FastAPI 应用创建、后台轮询任务、API 路由处理程序、模组状态聚合、请求/响应模型。 |
| `static/index.html` | 自包含前端（无需构建步骤）。纯 JS + CSS，通过 REST API 与后端通信。 |

---

## 关键模式

- **无服务层**：项目规模小，`main.py` 直接编排 AT 查询。如果查询逻辑增长，可抽取到 `queries/` 模块。

- **无数据库**：所有状态都是临时的（内存字典）。设备是数据源。

- **前端由 FastAPI 托管**：`StaticFiles` 挂载将 `static/` 提供在 `/` 路径。无需独立 Web 服务器。

---

## 命名约定

- Python 文件、函数、变量使用 `snake_case`。
- 类名使用 `PascalCase`。
- 模块级配置常量使用 `UPPER_SNAKE_CASE`（如 `PORT`、`BAUD`）。
- 前端：JS 函数和变量使用 `camelCase`，CSS 类名使用 `kebab-case`。

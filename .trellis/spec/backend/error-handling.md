# 错误处理

> 本项目如何处理错误。

---

## 概述

项目使用两层错误处理：

1. **AT 命令层**（`at_serial.py`）：自定义 `ATError` 表示模组通信失败。
2. **API 层**（`main.py`）：使用标准 HTTP 异常及状态码。

所有异常都被妥善处理——`main.py` 中每个 AT 命令调用都包裹在 try/except 中，每个 API 路由都返回有意义的 HTTP 错误。

---

## 错误类型

### ATError（at_serial.py）

当 AT 命令返回 `ERROR` 或超时时抛出。

```python
class ATError(Exception):
    def __init__(self, response: list[str], message: str = ...):
        self.response = response  # 导致错误的原始响应行
```

响应行被保留，以便调用方可以记录或展示原始模组输出。

参考：`at_serial.py:15-18`

### HTTPException（main.py）

标准 FastAPI HTTP 错误：

| HTTP 状态 | 使用场景 | 示例 |
|-----------|----------|------|
| 400 | AT 命令返回 ERROR | 无效的手机号码 |
| 500 | 意外的服务器错误 | 串口读取失败 |
| 503 | 模组未连接 | `/dev/ttyUSB2` 无设备 |

参考：`main.py:339-349`

---

## 错误处理模式

### AT 命令始终包裹 try/except

`main.py` 中每个 `modem.send_command(...)` 调用都包裹在 try/except 中。失败不会从辅助函数中未处理地传播：

```python
async def _query_sim_info():
    if not modem.connected:
        return {}
    result = {}
    try:
        r = await modem.send_command("AT+COPS?", timeout=3)
        ...
    except Exception:
        pass  # 单个查询失败不致命
    return result
```

参考：`main.py:116-181`

### API 路由将 ATError 映射为 HTTP 400

```python
try:
    ref = await modem.send_sms(req.number, req.text, timeout=60)
except ATError as e:
    raise HTTPException(400, str(e))
```

参考：`main.py:338-349`

### 后台任务容错

后台轮询任务（`_poll_modem_state`、`_refresh_sms_cache`）在顶层捕获所有异常，防止单次轮询周期导致任务退出：

```python
async def _poll_modem_state():
    while True:
        await asyncio.sleep(8)
        try:
            ...
        except Exception as e:
            logger.error("State poll error: %s", e)
```

参考：`main.py:199-234`

---

## 常见错误

- **让 AT 命令错误传播到 API 处理程序**：始终捕获 `ATError` 并转换为带有适当状态码的 `HTTPException`。切勿在未包裹状态码的情况下将原始模组错误文本暴露给客户端。

- **在后台任务中静默吞噬异常**：即使任务必须继续运行，也要用 `logger.error(...)` 记录异常。静默失败会导致串口问题无法调试。

- **阻塞事件循环**：不要在异步代码中使用 `time.sleep()`。使用 `asyncio.sleep()`，并通过 `time.monotonic()` 循环控制超时（如 `send_command` 和 `send_sms` 中的做法）。

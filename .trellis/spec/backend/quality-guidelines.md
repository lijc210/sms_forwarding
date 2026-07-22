# 质量规范

> 后端开发的代码质量标准。

---

## 禁止模式

1. **阻塞事件循环**：不要在异步函数中使用 `time.sleep()`、同步 `requests.get()` 或其他阻塞调用。使用 `asyncio.sleep()` 和异步库。参考：`at_serial.py:83-84` 使用 `time.monotonic()` 跟踪超时，而非 `time.sleep()`。

2. **无超时的串口读取**：不要在未设置超时的情况下从串口读取数据。所有读取都使用带显式超时的 `asyncio.wait_for()`。参考：`at_serial.py:61`、`at_serial.py:86`。

3. **无锁的全局可变状态**：`sms_cache` 由 `sms_cache_lock`（一个 `asyncio.Lock`）保护。后台任务和 API 处理程序都通过该锁访问。参考：`main.py:30-31`。

4. **后台任务中静默吞异常**：始终在异步后台循环中记录异常。参考：`main.py:233-234` 记录状态轮询错误。

---

## 必需模式

1. **每个 AT 命令调用都包裹在 try/except 中**：单个查询失败不能导致轮询周期崩溃。失败时返回空字典。参考：`main.py:116-181`。

2. **基于队列的串口读取**：不要直接在 `_read_lines` 之外读取 `StreamReader`。所有任务间通信使用 `_line_queue`。参考：`at_serial.py:58-71`。

3. **所有串口访问使用 async with lock**：每个串口命令必须获取 `_cmd_lock`。防止后台轮询器和 API 处理程序交错发送 AT 命令。参考：`at_serial.py:74`。

4. **通过环境变量配置**：`SMS_PORT` 和 `SMS_BAUD` 必须从环境变量读取，并具有合理的默认值。参考：`main.py:16-17`。

---

## 测试要求

- 目前没有配置测试框架。
- 添加测试时：
  - 使用 `pytest` + `pytest-asyncio` 支持异步测试。
  - 针对不需要物理设备的单元测试，Mock `serial_asyncio.open_serial_connection`。
  - 使用原始字符串样本（例如 `+CMGL` 响应样本）测试 AT 响应解析。
  - 使用 `asyncio` 事件循环控制测试基于定时器的超时行为。

---

## 性能考虑

- 后台轮询间隔：模组状态每 8 秒，短信缓存每 15 秒。这些间隔较为宽松，避免饱和串口。
- `_poll_modem_state` 中的 `asyncio.gather()` 调用同时运行 5 个独立的 AT 查询函数。这将会话轮询时间从顺序执行的约 15 秒减少到约 3 秒。
- 短信发送超时设为 60 秒，以适应长消息投递。

参考：`main.py:205-211`

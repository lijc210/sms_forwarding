# 日志规范

> 本项目如何进行日志记录。

---

## 日志库与配置

使用 Python 标准库 `logging`。配置在 `main.py` 顶部设置：

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
```

参考：`main.py:13`

每个模块创建自己的 logger：

```python
logger = logging.getLogger(__name__)
```

参考：`main.py:14`、`at_serial.py:12`

---

## 日志级别

| 级别 | 使用时机 | 示例 |
|------|----------|------|
| `DEBUG` | 详细的诊断信息，生产环境中较嘈杂 | AT 命令请求/响应对（`>>> AT+CIMI`） |
| `INFO` | 正常操作事件 | 连接/断开连接、启动 |
| `WARNING` | 不寻常但非错误的情况 | 启动时模组不可用 |
| `ERROR` | 可恢复的失败，需要调查 | 串口读取错误、状态轮询失败 |

参考：`at_serial.py:78`、`at_serial.py:44`、`main.py:302`、`main.py:234`

---

## 结构化日志

当前日志格式是带时间戳前缀的纯文本：

```
2026-07-22 11:52:02,769 [WARNING] main: Modem not available at /dev/ttyUSB2: ...
```

未使用 JSON 结构化日志。如需添加结构日志，建议保持相同的时间戳优先格式以保持一致性。

---

## 应记录的内容

| 事件 | 级别 | 位置 |
|------|------|------|
| 串口连接成功 | `INFO` | `at_serial.py:44` |
| 串口连接失败 | `WARNING` | `main.py:302` |
| AT 命令已发送 | `DEBUG` | `at_serial.py:78` |
| AT 命令响应 | `DEBUG` | `at_serial.py:...`（通过返回值隐式体现） |
| AT ERROR 响应 | `WARNING` | `at_serial.py:93` |
| 串口读取错误 | `ERROR` | `at_serial.py:67` |
| 模组状态刷新 | `DEBUG` | `main.py:232` |
| 状态轮询失败 | `ERROR` | `main.py:234` |
| 短信缓存刷新失败 | `DEBUG` | `main.py:248` |

---

## 禁止记录的内容

- **短信消息正文**：不要在任何级别记录消息正文——可能包含个人或敏感数据。仅记录短信元数据（数量、发送成功/失败、引用号）。
- **SIM 密钥**：不要在生产环境的 `INFO`/`WARNING` 日志中记录 IMSI、ICCID 或手机号码。DEBUF 级别在开发环境中可以接受。
- **包含消息正文的原始 AT 响应**：`+CMGL` 响应包含短信内容。仅记录解析后的短信索引和状态，不要记录完整响应。

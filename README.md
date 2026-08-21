# sms_forwarding（ML307A SMS Gateway）

基于中国移动 **ML307A 4G 模组** 的短信网关，通过串口 AT 指令控制模组，提供 Web 管理界面和 REST API。

## 功能介绍

- **短信收发**：查看收件箱、发送、删除短信，自动解码中文（UCS2）
- **状态监控**：模组、SIM、网络注册、信号质量（RSSI / RSRP 等）实时监测
- **余额查询**：USSD 码查询话费余额（默认 `*100#`）
- **号码保号**：定期通过蜂窝网络访问指定 URL 产生流量，防止号码被运营商回收
- **管理界面**：内置响应式 Web 页面，同时提供完整 REST API（访问 `/docs` 查看接口文档）

## 部署

需要一台直连 ML307A 模组串口的 Linux 主机（树莓派、开发板等），默认串口 `/dev/ttyUSB2`。

### 方式一：本地运行

需要 Python ≥ 3.11 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
uv run python -u main.py   # 默认监听 0.0.0.0:8000
```

### 方式二：Docker

直接使用 GitHub Actions 自动构建的镜像：

```bash
docker run -d --name sms-gateway \
  --device /dev/ttyUSB2 \
  -p 8000:8000 \
  --restart unless-stopped \
  ghcr.io/lijc210/sms_forwarding:latest
```

### 方式三：GitHub Actions 自动构建镜像（docker compose）

推送 `v*` 版本标签（如 `v1.0.0`）后，工作流（`.github/workflows/docker-build.yml`）自动构建 **amd64/arm64 多架构镜像** 并推送到 `ghcr.io`。**最新版本 tag 会自动更新 `latest`**。

项目根目录已提供 `docker-compose.yml`，将其中 `ghcr.io/<GitHub用户名>/sms_forwarding` 替换为实际镜像地址后运行：

```bash
# 拉取镜像并启动
 docker compose up -d

# 查看日志
 docker compose logs -f

# 更新到最新镜像
 docker compose pull && docker compose up -d
```

> 私有仓库拉取镜像需先登录：`docker login ghcr.io`。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SMS_PORT` | `/dev/ttyUSB2` | 模组串口设备路径 |
| `SMS_BAUD` | `115200` | 串口波特率 |
| `SMS_WEB_HOST` | `0.0.0.0` | Web 监听地址 |
| `SMS_WEB_PORT` | `8000` | Web 监听端口 |

启动后访问 `http://<主机IP>:8000` 打开管理界面。若串口无法打开（权限不足），将运行用户加入 `dialout` 组即可。

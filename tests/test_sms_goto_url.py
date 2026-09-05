#!/usr/bin/env python3
"""
通过 SIM 卡蜂窝流量访问网页（ML307A 模块 AT+MIP* TCP 指令）
测试网站: http://www.example.com/

原理：
ML307A-DSLN 固件不支持 AT+HTTPINIT 系列指令（实测返回 ERROR），
也不支持 SSL/TLS AT 指令，只提供 AT+MIPCALL/MIPOPEN/MIPSEND 底层
TCP 接口。因此本脚本通过 MIP 指令建立 TCP 连接后手动发送 HTTP/1.1
GET 请求，流量完全走 SIM 卡蜂窝网络，与本机 WiFi/以太网无关。

流程：
1. AT / AT+CGATT? / AT+MIPCALL?   确认模块就绪、已附着、已拿到 IP
2. AT+MIPOPEN=0,"TCP",<addr>,80   建立 TCP 连接（域名优先，DNS 失败
   则回退本地解析 IPv4 后直连）
3. AT+MIPSEND=0,<len> + 请求报文  发送 HTTP GET
4. 等待 +MIPURC: "rtcp",0,<len>,<data> 接收响应
5. AT+MIPCLOSE=0                  关闭连接

常见错误码：
  559 = TCP 连接失败（服务器不可达/被运营商拦截）
  580 = 域名 DNS 解析失败
  550 = socket 未打开就发送

使用前请确认：
1. 端口没有被其他程序占用（sudo lsof /dev/ttyUSB2 检查）
2. 已安装 pyserial
3. SIM 卡已注册网络且开通了数据流量（漫游卡需支持数据漫游）

用法：
.venv/bin/python tests/test_goto_url.py
.venv/bin/python tests/test_goto_url.py --url http://www.example.com/
"""

import argparse
import re
import socket
import sys
import time
from urllib.parse import urlsplit

import serial

PORT = "/dev/ttyUSB2"
BAUDRATE = 115200
DEFAULT_URL = "http://www.example.com/"
TIMEOUT = 30

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

SOCKET_ID = 0  # MIP 指令支持 0-5 共 6 路 socket，测试用 0 号


def send_at(ser, command, wait=1.0, timeout=None):
    """发送 AT 指令并返回原始响应文本。

    timeout 不为 None 时持续读取直到出现 OK/ERROR 或超时。
    """
    ser.reset_input_buffer()
    ser.write((command + "\r\n").encode())
    buf = ""
    if timeout is None:
        time.sleep(wait)
        buf = ser.read(ser.in_waiting or 4096).decode(errors="ignore")
    else:
        start = time.time()
        while time.time() - start < timeout:
            time.sleep(0.2)
            chunk = ser.read(ser.in_waiting or 4096)
            if chunk:
                buf += chunk.decode(errors="ignore")
            if "OK" in buf or "ERROR" in buf or "+CME ERROR" in buf:
                time.sleep(0.3)
                buf += ser.read(ser.in_waiting or 4096).decode(errors="ignore")
                break
    print(f"[调试] {command} -> {buf.strip()[:200]!r}")
    return buf


def wait_urc(ser, pattern, timeout):
    """等待匹配 pattern 的 URC 上报（如 +MIPOPEN: / +MIPURC:）"""
    rx = re.compile(pattern)
    buf = ""
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(0.3)
        chunk = ser.read(ser.in_waiting or 8192)
        if chunk:
            buf += chunk.decode(errors="ignore")
        m = rx.search(buf)
        if m:
            # 稍等片刻让同一 URC 的剩余数据到齐
            time.sleep(0.5)
            buf += ser.read(ser.in_waiting or 8192).decode(errors="ignore")
            return m, buf
    return None, buf


def tcp_connect(ser, host, port):
    """建立 TCP 连接：域名直连，DNS 失败(580)则本地解析 IPv4 后重试"""
    print(f"[连接] {host}:{port} ...")
    ser.reset_input_buffer()
    ser.write(f'AT+MIPOPEN={SOCKET_ID},"TCP","{host}",{port}\r\n'.encode())
    m, buf = wait_urc(ser, rf"\+MIPOPEN: {SOCKET_ID},(\d+)", 20)
    if m and m.group(1) == "0":
        print(f"[连接] 成功（域名直连）")
        return host

    err = m.group(1) if m else "超时"
    if err == "580":
        # 模块 DNS 解析失败，本地解析 IPv4 直连（仅获取地址用，流量仍走蜂窝）
        try:
            ip = socket.getaddrinfo(host, port, socket.AF_INET)[0][4][0]
        except socket.gaierror:
            print(f"[失败] 模块 DNS 失败(580) 且本地也解析不了 {host}")
            return None
        print(f"[连接] 模块 DNS 失败(580)，本地解析 {host} -> {ip}，改用 IP 直连")
        ser.reset_input_buffer()
        ser.write(f'AT+MIPOPEN={SOCKET_ID},"TCP","{ip}",{port}\r\n'.encode())
        m, buf = wait_urc(ser, rf"\+MIPOPEN: {SOCKET_ID},(\d+)", 20)
        if m and m.group(1) == "0":
            print("[连接] 成功（IP 直连）")
            return ip
        err = m.group(1) if m else "超时"

    print(f"[失败] TCP 连接失败，错误码 {err}"
          f"（559=服务器不可达/被拦截，580=DNS失败，可能是 SIM 未开通数据或漫游被限制）")
    return None


def http_get(ser, host, path):
    """发送 HTTP GET 并收集响应（经 +MIPURC "rtcp" 上报）"""
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"User-Agent: {USER_AGENT}\r\n"
        "Accept: text/html\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    ser.reset_input_buffer()
    ser.write(f"AT+MIPSEND={SOCKET_ID},{len(req)}\r\n".encode())
    # 等待 '>' 输入提示符
    prompt = ""
    start = time.time()
    while time.time() - start < 5:
        time.sleep(0.2)
        chunk = ser.read(ser.in_waiting or 256)
        if chunk:
            prompt += chunk.decode(errors="ignore")
        if ">" in prompt:
            break
    if ">" not in prompt:
        print(f"[失败] 未收到 '>' 提示符: {prompt.strip()!r}")
        return ""
    ser.write(req.encode())
    ser.flush()

    # 接收：+MIPURC: "rtcp",0,<len>,<data>，直到收到 </html> 或连接关闭
    body = ""
    start = time.time()
    while time.time() - start < TIMEOUT:
        time.sleep(0.3)
        chunk = ser.read(ser.in_waiting or 8192)
        if chunk:
            body += chunk.decode(errors="ignore")
        if "</html>" in body or "</HTML>" in body:
            break
        if "+MIPURC" in body and len(re.findall(r'\+MIPURC: "rtcp"', body)) > 3 and not chunk:
            break
    return body


def main():
    parser = argparse.ArgumentParser(description="通过 SIM 卡流量访问网页")
    parser.add_argument("--url", default=DEFAULT_URL, help="目标网址（本模块仅支持 http://）")
    parser.add_argument("--port", default=PORT, help="串口设备")
    parser.add_argument("--baudrate", type=int, default=BAUDRATE)
    args = parser.parse_args()

    parts = urlsplit(args.url)
    if parts.scheme == "https":
        print("[注意] ML307A-DSLN 固件无 SSL AT 指令，不支持 https://，自动降级为 http://")
        parts = parts._replace(scheme="http")
    if parts.scheme != "http":
        print(f"[失败] 不支持的协议: {parts.scheme}")
        return 1
    host = parts.hostname
    path = parts.path or "/"
    port = parts.port or 80

    try:
        ser = serial.Serial(args.port, args.baudrate, timeout=1)
    except serial.SerialException as e:
        print(f"打开串口失败: {e}")
        return 1

    ok = False
    try:
        print("[1/5] 测试模块通信...")
        resp = send_at(ser, "AT")
        if "OK" not in resp:
            print("模块无响应，请检查连接。原始返回：", repr(resp))
            return 1

        print("[2/5] 检查网络附着...")
        resp = send_at(ser, "AT+CGATT?")
        if "+CGATT: 1" not in resp:
            print("未附着分组网络（+CGATT != 1），请检查 SIM 卡状态")
            return 1

        print("[3/5] 检查 PDP 上下文...")
        resp = send_at(ser, "AT+MIPCALL?")
        m = re.search(r'\+MIPCALL: 1,1,"([\d.]+)"', resp)
        if m:
            print(f"[PDP] 已激活，IP: {m.group(1)}")
        else:
            # PDP 未激活则尝试激活（使用 SIM 卡已配置的 APN）
            resp = send_at(ser, "AT+CGACT=1,1", timeout=10)
            resp = send_at(ser, "AT+MIPCALL?")
            m = re.search(r'\+MIPCALL: 1,1,"([\d.]+)"', resp)
            if not m:
                print("PDP 激活失败，请检查 APN 配置（AT+CGDCONT?）")
                return 1
            print(f"[PDP] 已激活，IP: {m.group(1)}")

        send_at(ser, f"AT+MIPCLOSE={SOCKET_ID}", wait=0.5)  # 清理残留连接，忽略错误

        print("[4/5] 建立 TCP 连接...")
        addr = tcp_connect(ser, host, port)
        if not addr:
            return 1

        print("[5/5] 发送 HTTP GET 并接收响应...")
        body = http_get(ser, host, path)

        # 提取 +MIPURC 里的数据部分
        payload = "".join(
            re.findall(r'\+MIPURC: "rtcp",\d+,\d+,(.*)', body)
        )

        print("\n===== 最终结果 =====")
        sm = re.search(r"HTTP/[\d.]+\s+(\d{3})", body + payload)
        status_code = sm.group(1) if sm else "?"
        print(f"[状态码] {status_code}")
        tm = re.search(r"<title>(.*?)</title>", payload, re.S | re.I)
        print(f"[页面标题] {tm.group(1).strip() if tm else '(未找到)'}")
        print(f"[响应长度] {len(payload)} 字符")

        if status_code == "200":
            print("测试通过 ✓（流量经 SIM 卡蜂窝网络）")
            ok = True
        else:
            print("测试失败 ✗")
        print("\n[原始响应]")
        print((body + payload)[:800])

    finally:
        send_at(ser, f"AT+MIPCLOSE={SOCKET_ID}", wait=1)
        ser.close()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

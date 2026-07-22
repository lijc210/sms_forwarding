#!/usr/bin/env python3
"""
通过 AT 指令发送短信 "INFO" 到 85075（giffgaff 余额查询）
并监听、自动读取运营商的回复短信

使用前请确认：
1. 端口没有被其他程序占用（sudo lsof /dev/ttyUSB2 检查）
2. 已安装 pyserial: pip install pyserial --break-system-packages
"""

import serial
import time
import re
import sys

PORT = "/dev/ttyUSB2"
BAUDRATE = 115200
TARGET_NUMBER = "85075"
SMS_TEXT = "INFO"
WAIT_REPLY_SECONDS = 60  # 等待回复短信的最长时间


def send_at(ser, command, wait=1.0):
    """发送一条普通 AT 指令，返回响应文本"""
    ser.reset_input_buffer()
    ser.write((command + "\r\n").encode())
    time.sleep(wait)
    return ser.read(ser.in_waiting or 1024).decode(errors="ignore")


def send_sms(ser, number, text, timeout=20):
    """
    发送短信，使用文本模式 AT+CMGS
    返回 (成功与否, 原始响应)
    """
    ser.reset_input_buffer()
    cmd = f'AT+CMGS="{number}"'
    ser.write((cmd + "\r\n").encode())

    # 等待模块返回 '>' 提示符，才能输入短信正文
    start = time.time()
    buf = ""
    while time.time() - start < timeout:
        time.sleep(0.2)
        chunk = ser.read(ser.in_waiting or 256).decode(errors="ignore")
        buf += chunk
        if ">" in buf:
            break
    else:
        return False, buf + "\n[超时：未收到 '>' 提示符]"

    # 稍作等待，确保模块已切换到接收正文状态
    time.sleep(0.3)

    # 输入短信正文，以 Ctrl+Z (0x1A) 结束
    ser.write(text.encode())
    ser.flush()
    time.sleep(0.1)
    ser.write(bytes([0x1A]))
    ser.flush()

    # 等待发送结果 OK / ERROR（短信发送通常需要更长时间，最长等 30 秒）
    start = time.time()
    send_timeout = max(timeout, 30)
    while time.time() - start < send_timeout:
        time.sleep(0.3)
        chunk = ser.read(ser.in_waiting or 256)
        if chunk:
            buf += chunk.decode(errors="ignore")
            print(f"[调试] 收到片段: {chunk!r}")
        if "OK" in buf or "ERROR" in buf or "+CMS ERROR" in buf:
            break

    success = "OK" in buf and "ERROR" not in buf
    return success, buf


def decode_ucs2(hex_str):
    """尝试把 UCS2 十六进制字符串解码成可读文本，失败则原样返回"""
    try:
        return bytes.fromhex(hex_str.strip()).decode("utf-16-be")
    except Exception:
        return hex_str


def wait_for_reply(ser, timeout=WAIT_REPLY_SECONDS):
    """
    监听串口，等待 +CMTI 新短信提示，自动读取该短信内容
    """
    print(f"等待回复短信（最长 {timeout} 秒）...")
    start = time.time()
    buf = ""
    while time.time() - start < timeout:
        chunk = ser.read(ser.in_waiting or 256)
        if chunk:
            buf += chunk.decode(errors="ignore")

        match = re.search(r'\+CMTI:\s*"[^"]+",(\d+)', buf)
        if match:
            index = match.group(1)
            print(f"收到新短信提醒，索引号 {index}，正在读取...")
            time.sleep(0.5)
            resp = send_at(ser, f"AT+CMGR={index}", wait=1.0)
            return resp
        time.sleep(0.5)

    return None


def main():
    try:
        ser = serial.Serial(PORT, BAUDRATE, timeout=1)
    except serial.SerialException as e:
        print(f"打开串口失败: {e}")
        print("请检查端口是否被其他程序占用，或路径是否正确。")
        sys.exit(1)

    try:
        print("[1/5] 测试模块通信...")
        resp = send_at(ser, "AT")
        if "OK" not in resp:
            print("模块无响应，请检查连接。原始返回：", repr(resp))
            return

        print("[2/5] 设置短信文本模式...")
        send_at(ser, "AT+CMGF=1")

        print("[3/5] 开启新短信主动上报...")
        send_at(ser, "AT+CNMI=2,1,0,0,0")

        print(f"[4/5] 发送短信 \"{SMS_TEXT}\" 到 {TARGET_NUMBER} ...")
        ok, raw = send_sms(ser, TARGET_NUMBER, SMS_TEXT)
        if not ok:
            print("短信发送失败，原始返回：")
            print(raw)
            return
        print("短信发送成功，原始返回：")
        print(raw)

        print("[5/5] 等待运营商回复...")
        reply = wait_for_reply(ser)

        if not reply:
            print(f"\n在 {WAIT_REPLY_SECONDS} 秒内未收到回复短信，"
                  f"可以稍后手动执行 AT+CMGL=\"ALL\" 查看。")
            return

        print("\n===== 收到的回复原始内容 =====")
        print(reply)

        # 尝试提取短信正文（+CMGR 返回的第二行通常是正文）
        lines = [l for l in reply.splitlines() if l.strip()]
        body_lines = [l for l in lines if not l.startswith("+CMGR") and l.strip() != "OK"]
        if body_lines:
            body = "\n".join(body_lines)
            print("\n===== 短信正文 =====")
            # 判断是否为 UCS2 十六进制（全是十六进制字符且长度为偶数）
            if re.fullmatch(r'[0-9A-Fa-f]+', body.strip()) and len(body.strip()) % 4 == 0:
                print(decode_ucs2(body))
            else:
                print(body)

    finally:
        ser.close()


if __name__ == "__main__":
    main()

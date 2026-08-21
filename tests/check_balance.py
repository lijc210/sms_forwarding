#!/usr/bin/env python3
"""
通过 AT 指令查询话费余额（USSD 方式）
适用于 ML307A 等支持 AT+CUSD 的 4G 模块

使用前请确认：
1. 端口没有被其他程序占用（可用 `sudo lsof /dev/ttyUSB2` 检查）
2. 已安装 pyserial: pip install pyserial --break-system-packages
"""

import serial
import time
import sys

PORT = "/dev/ttyUSB2"
BAUDRATE = 115200
USSD_CODE = "*100#"  # 按运营商实际余额查询码修改，比如 giffgaff 是 *100#


def send_at(ser, command, wait=1.0):
    """发送一条 AT 指令，返回模块响应的原始文本"""
    ser.reset_input_buffer()
    ser.write((command + "\r\n").encode())
    time.sleep(wait)
    resp = ser.read(ser.in_waiting or 1024).decode(errors="ignore")
    return resp.strip()


def decode_ucs2(hex_str):
    """把 UCS2 十六进制字符串解码成可读文本"""
    hex_str = hex_str.strip()
    try:
        return bytes.fromhex(hex_str).decode("utf-16-be")
    except Exception:
        return hex_str  # 解码失败就原样返回


def query_balance(port=PORT, baudrate=BAUDRATE, ussd_code=USSD_CODE):
    try:
        ser = serial.Serial(port, baudrate, timeout=1)
    except serial.SerialException as e:
        print(f"打开串口失败: {e}")
        print("请检查端口是否被其他程序占用，或路径是否正确。")
        sys.exit(1)

    try:
        print(f"[1/3] 测试模块通信...")
        resp = send_at(ser, "AT")
        if "OK" not in resp:
            print("模块无响应，请检查连接。原始返回：", repr(resp))
            return

        print(f"[2/3] 开启 USSD 上报...")
        send_at(ser, "AT+CUSD=1")

        print(f"[3/3] 发送查询码 {ussd_code} ...")
        resp = send_at(ser, f'AT+CUSD=1,"{ussd_code}",15', wait=6)

        if "+CUSD" not in resp:
            print("未收到 USSD 返回，可能需要更长等待时间，原始返回：")
            print(resp)
            print("尝试再等待几秒重新读取...")
            time.sleep(4)
            extra = ser.read(ser.in_waiting or 1024).decode(errors="ignore")
            resp += extra

        print("\n===== 原始返回 =====")
        print(resp)

        # 尝试解析 +CUSD: <status>,"<content>",<dcs>
        if "+CUSD:" in resp:
            line = [l for l in resp.splitlines() if "+CUSD:" in l][0]
            try:
                # 形如 +CUSD: 0,"内容",15  或  +CUSD: 0,"48656C6C6F",72(UCS2场景dcs常见72/68)
                parts = line.split(":", 1)[1].strip()
                status_str, remainder = parts.split(",", 1)
                content = remainder.rsplit(",", 1)[0].strip().strip('"')
                dcs = remainder.rsplit(",", 1)[-1].strip()

                print("\n===== 解析结果 =====")
                if dcs in ("15", "0"):
                    # 纯文本编码，直接显示
                    print("查询结果:", content)
                else:
                    # 大概率是 UCS2 编码的十六进制串
                    decoded = decode_ucs2(content)
                    print("查询结果 (UCS2解码):", decoded)
            except Exception as e:
                print(f"解析失败({e})，请查看上方原始返回自行判断内容。")
        else:
            print("\n未能获取到有效的 USSD 返回，请确认该 SIM 卡是否支持此查询码，"
                  "或者稍后重试。")

    finally:
        ser.close()


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else USSD_CODE
    query_balance(ussd_code=code)
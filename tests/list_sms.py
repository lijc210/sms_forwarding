#!/usr/bin/env python3
"""
查询短信列表（AT+CMGL）
支持文本模式读取，自动识别并解码 UCS2 编码的短信内容

使用前请确认：
1. 端口没有被其他程序占用（sudo lsof /dev/ttyUSB2 检查）
2. 已安装 pyserial: pip install pyserial --break-system-packages
"""

import serial
import time
import re
import sys
import argparse

PORT = "/dev/ttyUSB2"
BAUDRATE = 115200


def send_at(ser, command, wait=1.5):
    ser.reset_input_buffer()
    ser.write((command + "\r\n").encode())
    time.sleep(wait)
    return ser.read(ser.in_waiting or 4096).decode(errors="ignore")


def decode_ucs2(hex_str):
    """尝试把 UCS2 十六进制字符串解码成可读文本，失败则原样返回"""
    hex_str = hex_str.strip()
    if re.fullmatch(r'[0-9A-Fa-f]+', hex_str) and len(hex_str) % 4 == 0:
        try:
            return bytes.fromhex(hex_str).decode("utf-16-be")
        except Exception:
            pass
    return hex_str


def parse_sms_list(raw_text):
    """
    解析 AT+CMGL 文本模式返回的短信列表
    格式：
    +CMGL: <index>,"<status>","<number>",,"<date,time>"
    <content>
    +CMGL: ...
    <content>
    ...
    OK
    """
    messages = []
    lines = raw_text.splitlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("+CMGL:"):
            header = line[len("+CMGL:"):].strip()
            # 用简单的正则拆分逗号分隔字段（考虑到字段用引号包裹）
            fields = re.findall(r'"([^"]*)"|(\d+)', header)
            # 展开匹配结果（正则会产生两个分组，取非空的）
            parsed_fields = [a if a else b for a, b in fields]

            index = parsed_fields[0] if len(parsed_fields) > 0 else "?"
            status = parsed_fields[1] if len(parsed_fields) > 1 else "?"
            number = parsed_fields[2] if len(parsed_fields) > 2 else "?"
            date_time = parsed_fields[-1] if len(parsed_fields) > 3 else "?"

            content = ""
            if i + 1 < len(lines) and not lines[i + 1].startswith("+CMGL:") and lines[i + 1].strip() != "OK":
                content = lines[i + 1].strip()
                i += 1

            decoded_number = decode_ucs2(number) if re.fullmatch(r'[0-9A-Fa-f]+', number) else number
            decoded_content = decode_ucs2(content)

            messages.append({
                "index": index,
                "status": status,
                "number": decoded_number,
                "datetime": date_time,
                "content": decoded_content,
            })
        i += 1

    return messages


def main():
    parser = argparse.ArgumentParser(description="查询短信列表")
    parser.add_argument(
        "filter", nargs="?", default="ALL",
        choices=["ALL", "REC UNREAD", "REC READ", "STO UNSENT", "STO SENT"],
        help="筛选条件（默认 ALL），可选: ALL / REC UNREAD / REC READ / STO UNSENT / STO SENT"
    )
    parser.add_argument("--port", default=PORT)
    parser.add_argument("--baudrate", type=int, default=BAUDRATE)
    parser.add_argument("--raw", action="store_true", help="只打印原始返回，不做解析")
    args = parser.parse_args()

    try:
        ser = serial.Serial(args.port, args.baudrate, timeout=1)
    except serial.SerialException as e:
        print(f"打开串口失败: {e}")
        sys.exit(1)

    try:
        resp = send_at(ser, "AT")
        if "OK" not in resp:
            print("模块无响应，请检查连接。原始返回：", repr(resp))
            return

        send_at(ser, "AT+CMGF=1")  # 文本模式

        cmd = f'AT+CMGL="{args.filter}"'
        raw = send_at(ser, cmd, wait=2.0)

        if args.raw:
            print(raw)
            return

        messages = parse_sms_list(raw)

        if not messages:
            print(f"没有找到符合条件（{args.filter}）的短信。")
            print("\n原始返回：")
            print(raw)
            return

        print(f"共找到 {len(messages)} 条短信：\n")
        for m in messages:
            print("=" * 50)
            print(f"索引号  : {m['index']}")
            print(f"状态    : {m['status']}")
            print(f"发件号码: {m['number']}")
            print(f"时间    : {m['datetime']}")
            print(f"内容    : {m['content']}")
        print("=" * 50)

    finally:
        ser.close()


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
通过 PDU 模式发送中文（UCS2 编码）短信
适用于只支持 IRA 字符集、无法直接在文本模式发中文的模块（如 ML307A）

使用前请确认：
1. 端口没有被其他程序占用（sudo lsof /dev/ttyUSB2 检查）
2. 已安装 pyserial: pip install pyserial --break-system-packages

原理：
文本模式(AT+CMGF=1)受限于 AT+CSCS 字符集，只能发 ASCII。
PDU 模式(AT+CMGF=0)则是直接构造二进制短信协议帧，正文用 UCS2 编码，
不受 AT 层字符集限制，可以正常发送中文。
"""

import serial
import time
import sys
import argparse

PORT = "/dev/ttyUSB2"
BAUDRATE = 115200


def send_at(ser, command, wait=1.0):
    ser.reset_input_buffer()
    ser.write((command + "\r\n").encode())
    time.sleep(wait)
    return ser.read(ser.in_waiting or 1024).decode(errors="ignore")


def encode_number(number: str) -> tuple[str, str]:
    """
    编码目标号码为 PDU 格式
    返回 (号码长度的十进制字符串, TOA+号码的十六进制字符串)
    """
    toa = "91" if number.startswith("+") else "81"  # 91=国际格式, 81=未知格式
    digits = number.lstrip("+")

    length = len(digits)  # 号码位数（不含+号），十进制
    # 号码按半字节颠倒配对编码（BCD反转），奇数位补F
    if len(digits) % 2 != 0:
        digits += "F"
    swapped = "".join(digits[i + 1] + digits[i] for i in range(0, len(digits), 2))

    return f"{length:02X}", toa + swapped


def build_pdu(number: str, text: str) -> tuple[str, int]:
    """
    构造 UCS2 编码的短信 PDU
    返回 (pdu十六进制字符串, TPDU长度-发送AT+CMGS时需要用到的第二个参数)
    """
    smsc_prefix = "00"  # 00 表示使用模块当前配置的短信中心号码

    first_octet = "11"  # SMS-SUBMIT, TP-VP字段存在(相对格式)
    message_ref = "00"  # 消息参考号，模块自动分配可填00

    addr_len, addr_pdu = encode_number(number)

    pid = "00"       # 协议标识
    dcs = "08"        # 数据编码方案: 08 = UCS2

    vp = "AA"          # 有效期，AA约等于4天（相对时间格式，可不太关心）

    ucs2_bytes = text.encode("utf-16-be")
    ucs2_hex = ucs2_bytes.hex().upper()
    udl = f"{len(ucs2_bytes):02X}"  # 用户数据长度，按字节数（UCS2下）

    tpdu = first_octet + message_ref + addr_len + addr_pdu + pid + dcs + vp + udl + ucs2_hex

    pdu = smsc_prefix + tpdu
    tpdu_length = len(tpdu) // 2  # AT+CMGS 的长度参数，不含 SMSC 部分，按字节数计算

    return pdu, tpdu_length


def send_pdu_sms(ser, number, text, timeout=30):
    pdu, tpdu_len = build_pdu(number, text)

    print(f"[调试] 构造的PDU: {pdu}")
    print(f"[调试] TPDU长度: {tpdu_len}")

    ser.reset_input_buffer()
    cmd = f"AT+CMGS={tpdu_len}"
    ser.write((cmd + "\r\n").encode())

    # 等待 '>' 提示符
    start = time.time()
    buf = ""
    got_prompt = False
    while time.time() - start < 15:
        time.sleep(0.2)
        chunk = ser.read(ser.in_waiting or 256)
        if chunk:
            decoded = chunk.decode(errors="ignore")
            buf += decoded
            print(f"[调试] 收到: {chunk!r}")
        if ">" in buf:
            got_prompt = True
            break

    if not got_prompt:
        return False, buf + "\n[超时：未收到 '>' 提示符]"

    time.sleep(0.3)

    ser.write(pdu.encode())
    ser.flush()
    time.sleep(0.1)
    ser.write(bytes([0x1A]))  # Ctrl+Z 结束
    ser.flush()

    start = time.time()
    while time.time() - start < timeout:
        time.sleep(0.3)
        chunk = ser.read(ser.in_waiting or 256)
        if chunk:
            decoded = chunk.decode(errors="ignore")
            buf += decoded
            print(f"[调试] 收到: {chunk!r}")
        if "OK" in buf or "ERROR" in buf:
            break

    success = "OK" in buf and "ERROR" not in buf
    return success, buf


def main():
    parser = argparse.ArgumentParser(description="通过 PDU 模式发送中文短信")
    parser.add_argument("number", help="目标手机号码，如 +8619901718151")
    parser.add_argument("text", help="短信内容（支持中文）")
    parser.add_argument("--port", default=PORT)
    parser.add_argument("--baudrate", type=int, default=BAUDRATE)
    args = parser.parse_args()

    try:
        ser = serial.Serial(args.port, args.baudrate, timeout=1)
    except serial.SerialException as e:
        print(f"打开串口失败: {e}")
        sys.exit(1)

    try:
        print("[1/3] 测试模块通信...")
        resp = send_at(ser, "AT")
        if "OK" not in resp:
            print("模块无响应，请检查连接。原始返回：", repr(resp))
            return

        print("[2/3] 切换到 PDU 模式...")
        resp = send_at(ser, "AT+CMGF=0")
        if "OK" not in resp:
            print("切换 PDU 模式失败：", resp)
            return

        print(f"[3/3] 发送短信到 {args.number}: {args.text}")
        ok, raw = send_pdu_sms(ser, args.number, args.text)

        print("\n===== 最终结果 =====")
        print("✅ 发送成功！" if ok else "❌ 发送失败。")
        print("原始返回：")
        print(raw)

    finally:
        # 恢复文本模式，避免影响后续其他脚本
        send_at(ser, "AT+CMGF=1")
        ser.close()


if __name__ == "__main__":
    main()

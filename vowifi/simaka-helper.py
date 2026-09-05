#!/root/workspace/workexplore/sms_forwarding/.venv/bin/python
"""simaka-helper: strongSwan modem-simaka 插件的串口助手进程。

用法: simaka-helper aka <RAND_hex> <AUTN_hex>
输出(stdout, 单行):
    OK <RES_hex> <CK_hex> <IK_hex>   鉴权成功
    AUTS <hex14字节>                 SQN 同步失败（重同步）
其他情况写 stderr 并以非零退出码结束。

环境变量:
    SIMAKA_PORT  串口设备            (默认 /dev/ttyUSB2)
    SIMAKA_AID   USIM AID            (默认 A0000000871002FF44FFFF89062100FF)
    SIMAKA_P2    AUTHENTICATE P2     (默认 81 = AKA'; 可设 00 = 普通 AKA)
    SIMAKA_DEBUG 置 1 输出调试到 stderr

APDU 与响应解析格式与 simplus (ml307a_simaka.go) 完全一致:
    00 88 00 <P2> 22 10<RAND> 10<AUTN>   经 CGLA 逻辑通道发送
    成功: DB <len> RES(4-16) <16>CK <16>IK [<8>Kc]
    同步失败: SW=9862, 4D <14> AUTS
"""
import os
import re
import sys
import time

import serial

PORT = os.environ.get("SIMAKA_PORT", "/dev/ttyUSB2")
AID = os.environ.get("SIMAKA_AID", "A0000000871002FF44FFFF89062100FF")
P2 = os.environ.get("SIMAKA_P2", "81").strip()
DEBUG = os.environ.get("SIMAKA_DEBUG", "") == "1"


def dbg(msg):
    if DEBUG:
        print(msg, file=sys.stderr, flush=True)


def send_at(ser, cmd, wait=1.0, timeout=8.0):
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    buf = ""
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(0.15)
        chunk = ser.read(ser.in_waiting or 4096)
        if chunk:
            buf += chunk.decode(errors="ignore")
        if "OK" in buf or "ERROR" in buf:
            time.sleep(0.15)
            buf += ser.read(ser.in_waiting or 4096).decode(errors="ignore")
            break
    dbg(f"[AT] {cmd[:60]} -> {buf.strip()[:160]!r}")
    return buf


def ccho(ser):
    """打开逻辑通道。ML307A 返回裸数字而非 +CCHO: 格式，两种都兼容。"""
    resp = send_at(ser, f'AT+CCHO="{AID}"', wait=2)
    m = re.search(r"\+CCHO:\s*(\d+)", resp) or re.search(
        r"^\s*([0-9]+)\s*$", resp.strip(), re.M
    )
    if not m:
        raise RuntimeError(f"CCHO failed: {resp.strip()!r}")
    return int(m.group(1))


def cgla(ser, channel, apdu_hex):
    """逻辑通道发 APDU（CLA 自动替换为通道号），处理 61xx/9Fxx 跟随读取。"""
    apdu = f"{channel:02X}" + apdu_hex[2:]
    resp = send_at(ser, f'AT+CGLA={channel},{len(apdu)},"{apdu}"', wait=3)
    m = re.search(r'\+CGLA:\s*\d+,\s*"([0-9A-Fa-f]*)"', resp)
    if not m:
        raise RuntimeError(f"CGLA failed: {resp.strip()!r}")
    data = m.group(1).upper()
    sw = data[-4:]
    body = data[:-4]
    if sw[:2] in ("61", "9F"):
        le = sw[2:]
        resp2 = send_at(ser, f'AT+CGLA={channel},10,"{channel:02X}C00000{le}"', wait=2)
        m2 = re.search(r'\+CGLA:\s*\d+,\s*"([0-9A-Fa-f]*)"', resp2)
        if m2:
            d2 = m2.group(1).upper()
            body += d2[:-4]
            sw = d2[-4:]
    return body, sw


def take_field(data, pos, minimum, maximum):
    """simplus takeUSIMAKAField: <len> <value>，越界/长度异常返回 None"""
    if pos >= len(data):
        return None
    length = data[pos]
    pos += 1
    if length < minimum or length > maximum or pos + length > len(data):
        return None
    return data[pos : pos + length]


def main():
    if len(sys.argv) != 4 or sys.argv[1] != "aka":
        print("usage: simaka-helper aka <RAND_hex> <AUTN_hex>", file=sys.stderr)
        return 2
    rand, autn = sys.argv[2].upper(), sys.argv[3].upper()
    if len(rand) != 32 or len(autn) != 32:
        print("RAND/AUTN must be 16 bytes hex", file=sys.stderr)
        return 2

    ser = serial.Serial(PORT, 115200, timeout=1)
    try:
        send_at(ser, "AT", wait=0.5)
        ch = ccho(ser)
        try:
            apdu = f"008800{P2}2210{rand}10{autn}"
            body, sw = cgla(ser, ch, apdu)
        finally:
            send_at(ser, f"AT+CCHC={ch}", wait=0.5)
    finally:
        ser.close()

    dbg(f"[AKA] sw={sw} body={body}")
    raw = bytes.fromhex(body) if body else b""

    if sw == "9862" or (raw and raw[0] == 0x4D):
        # 同步失败: 4D <len=14> AUTS（兼容部分卡的裸返回）
        if raw and raw[0] == 0x4D:
            auts = take_field(raw, 1, 14, 14)
            if auts:
                print("AUTS " + auts.hex().upper(), flush=True)
                return 0
        print("AUTS 0000000000000000000000000000", flush=True)
        return 0

    if sw == "9000" and raw and raw[0] == 0xDB:
        res = take_field(raw, 1, 4, 16)
        if res is None:
            print(f"malformed DB response: {body}", file=sys.stderr)
            return 1
        pos = 2 + len(res)
        ck = take_field(raw, pos, 16, 16)
        if ck is None:
            print(f"malformed DB response: {body}", file=sys.stderr)
            return 1
        pos += 1 + 16
        ik = take_field(raw, pos, 16, 16)
        if ik:
            print(
                f"OK {res.hex().upper()} {ck.hex().upper()} {ik.hex().upper()}",
                flush=True,
            )
            return 0
        print(f"malformed DB response: {body}", file=sys.stderr)
        return 1

    print(f"AKA rejected: sw={sw} data={body}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

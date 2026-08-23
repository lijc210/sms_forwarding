"""短信 PDU 解析模块（GSM 03.40 / GSM 03.38）

模块的文本模式（AT+CMGF=1）无法正确呈现部分短信：
- 带 UDH 的长短信会被整体输出为十六进制串
- 字母数字发件人地址（如 "Lebara"）可能被错误解码

本模块在 PDU 模式（AT+CMGF=0）下自行解析 SMS-DELIVER，支持：
- GSM 7-bit 打包文本（含 UDH 填充位对齐）
- UCS-2 编码文本
- 数字 / 字母数字发件人地址
- SCTS 服务中心时间戳
- 长短信（级联短信）分段信息
"""

# GSM 03.38 7-bit 默认字母表（0x00-0x7F，0x1B 为扩展表转义符）
GSM7_ALPHABET = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

# 扩展表（0x1B 后跟随的字节 → 字符）
GSM7_EXT = {
    0x0A: "\n",
    0x0D: "\r",
    0x14: "^",
    0x1B: " ",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "€",
}

assert len(GSM7_ALPHABET) == 128

# BCD 半字节中超出 0-9 的特殊取值
_BCD_SPECIAL = {0xA: "*", 0xB: "#", 0xC: "a", 0xD: "b", 0xE: "c"}


class PDUParseError(ValueError):
    """PDU 格式无法解析"""


def unpack_gsm7(data: bytes, start_bit: int, septet_count: int) -> str:
    """从 data 的 start_bit 位（LSB 优先）起解包 septet_count 个 GSM7 字符"""
    values = []
    for k in range(septet_count):
        base = start_bit + k * 7
        v = 0
        for j in range(7):
            idx = base + j
            byte_i = idx >> 3
            if byte_i >= len(data):
                break
            v |= ((data[byte_i] >> (idx & 7)) & 1) << j
        values.append(v)

    chars = []
    i = 0
    while i < len(values):
        v = values[i]
        if v == 0x1B and i + 1 < len(values) and values[i + 1] in GSM7_EXT:
            chars.append(GSM7_EXT[values[i + 1]])
            i += 2
        else:
            chars.append(GSM7_ALPHABET[v] if v < 128 else "")
            i += 1
    return "".join(chars)


def decode_address(field: bytes) -> str:
    """解码 PDU 地址字段（[长度半字节数, TON/NPI, 编码数据...]）"""
    if len(field) < 2:
        return ""
    nibbles = field[0]
    if nibbles == 0:
        return ""
    toa = field[1]
    digits = field[2 : 2 + (nibbles + 1) // 2]
    ton = (toa >> 4) & 0x7

    if ton == 0x5:
        # 字母数字地址：GSM7 打包
        return unpack_gsm7(digits, 0, nibbles * 4 // 7).rstrip("\x00")

    # 数字地址：BCD 半字节交换（0xF 为尾部填充位）
    out = []
    for b in digits:
        for nib in (b & 0xF, (b >> 4) & 0xF):
            if len(out) >= nibbles:
                break
            if nib == 0xF:
                continue
            out.append(_BCD_SPECIAL.get(nib, str(nib)))
    s = "".join(out)
    if ton == 0x1:
        s = "+" + s
    return s


def decode_scts(data: bytes) -> str:
    """解码 7 字节 SCTS 时间戳，输出 "yy/MM/dd, HH:mm:ss +ZZ" 格式（本地时间+时区）"""
    if len(data) < 7:
        return ""
    semi = []
    for b in data[:7]:
        semi.append(b & 0xF)
        semi.append((b >> 4) & 0xF)

    def pair(a: int, b_: int) -> int:
        return a * 10 + b_

    tz_quarters = (semi[12] & 0x7) * 10 + semi[13]
    tz_sign = "-" if semi[12] & 0x8 else "+"
    tz_minutes = tz_quarters * 15
    if tz_minutes % 60 == 0:
        tz = f"{tz_sign}{tz_minutes // 60:02d}"
    else:
        tz = f"{tz_sign}{tz_minutes // 60:02d}:{tz_minutes % 60:02d}"

    return (
        f"{pair(semi[0], semi[1]):02d}/{pair(semi[2], semi[3]):02d}/{pair(semi[4], semi[5]):02d}, "
        f"{pair(semi[6], semi[7]):02d}:{pair(semi[8], semi[9]):02d}:{pair(semi[10], semi[11]):02d} {tz}"
    )


def _alphabet_bits(dcs: int) -> int:
    """根据 DCS 返回字符编码：0=GSM7, 1=8bit, 2=UCS-2"""
    if (dcs & 0xC0) == 0x00:
        return (dcs >> 2) & 0x03
    if 0xC0 <= dcs <= 0xCF or 0xE0 <= dcs <= 0xEF:
        return 0
    if 0xD0 <= dcs <= 0xDF:
        return 2
    # 0xF0-0xFF
    return 1 if dcs & 0x08 else 0


def _parse_udh(ud: bytes) -> tuple[int, dict | None]:
    """解析 UD 开头的 UDH，返回 (UDH 总字节数, 级联信息或 None)"""
    udhl = ud[0]
    if udhl == 0 or 1 + udhl > len(ud):
        return 0, None
    header = ud[1 : 1 + udhl]
    concat = None
    i = 0
    while i + 1 < len(header):
        iei, iel = header[i], header[i + 1]
        info = header[i + 2 : i + 2 + iel]
        if iei == 0x00 and iel >= 3:
            concat = {"ref": info[0], "total": info[1], "seq": info[2]}
        elif iei == 0x08 and iel >= 4:
            concat = {"ref": (info[0] << 8) | info[1], "total": info[2], "seq": info[3]}
        i += 2 + iel
    return 1 + udhl, concat


def parse_deliver(pdu_hex: str) -> dict:
    """解析 SMS-DELIVER PDU（十六进制串，含 SMSC 地址部分）

    返回 {"number", "date", "text", "part"}，part 为长短信分段 "n/m" 或 None。
    非 SMS-DELIVER（如状态报告）抛出 PDUParseError。
    """
    try:
        b = bytes.fromhex(pdu_hex.strip())
    except ValueError as e:
        raise PDUParseError(f"PDU 非合法十六进制: {e}") from e

    pos = 0
    if not b:
        raise PDUParseError("空 PDU")
    smsc_len = b[0]
    pos = 1 + smsc_len
    if pos >= len(b):
        raise PDUParseError("PDU 过短")

    first = b[pos]
    pos += 1
    if first & 0x03 != 0x00:
        raise PDUParseError(f"非 SMS-DELIVER 报文 (first octet={first:#04x})")

    # 发件人地址
    if pos >= len(b):
        raise PDUParseError("PDU 缺少发件人地址")
    addr_nibbles = b[pos]
    addr_size = 2 + (addr_nibbles + 1) // 2
    number = decode_address(b[pos : pos + addr_size])
    pos += addr_size

    if pos + 10 > len(b):
        raise PDUParseError("PDU 缺少 PID/DCS/SCTS")
    pos += 1  # PID
    dcs = b[pos]
    pos += 1
    date = decode_scts(b[pos : pos + 7])
    pos += 7

    udl = b[pos]
    pos += 1
    ud = b[pos:]

    has_udh = bool(first & 0x40)
    header_len = 0
    concat = None
    if has_udh and ud:
        header_len, concat = _parse_udh(ud)

    alphabet = _alphabet_bits(dcs)
    if alphabet == 2:
        payload = ud[header_len:]
        text = payload.decode("utf-16-be", errors="replace")
    elif alphabet == 0:
        if header_len:
            header_bits = header_len * 8
            fill = (7 - header_bits % 7) % 7
            start_bit = header_bits + fill
            septets = udl - (header_bits + fill) // 7
        else:
            start_bit = 0
            septets = udl
        text = unpack_gsm7(ud, start_bit, max(septets, 0))
    else:
        # 8-bit 数据无法按文本呈现，回退显示十六进制
        text = ud[header_len:].hex()

    return {
        "number": number,
        "date": date,
        "text": text,
        "part": f"{concat['seq']}/{concat['total']}" if concat else None,
    }

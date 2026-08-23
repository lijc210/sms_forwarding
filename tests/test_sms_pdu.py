#!/usr/bin/env python3
"""sms_pdu 模块单元测试

使用真实设备（ML307A 文本模式下乱码）的两条长短信分段数据构造完整
SMS-DELIVER PDU，验证 PDU 模式解析能正确还原发件人与正文。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sms_pdu import decode_address, decode_scts, merge_concat_parts, parse_deliver

# 截图中第 1 条乱码短信的用户数据（长短信第 2/2 段，GSM7 + UDH）
UD_PART2 = "0500032802025CF5F5ABEC7E5DD3E6F438CC66A7DD67"
# 截图中第 2 条乱码短信的用户数据（长短信第 1/2 段）
UD_PART1 = (
    "050003280201906536FBCD025DD3C634681C66B3D3EE3328EC26836847D038CC66A7DD67"
    "10CAFA66528BA07198CD4EBBCF29507A0E72BFEFA0B03D9C6687C5EC32E8ED06E5DF753"
    "988591687E56150FB2D4EB3CB2077BD2D2ECB5D20EA1B342FD341693AA80E07BDDDA0F"
    "CBB2E0791CBF6F4B80CB2A7E7693A084DA7C3E7BAD7EB7EBFBBDB86571581E768DDF"
)
if len(UD_PART1) % 2:
    UD_PART1 += "0"


def build_deliver(ud_hex: str, sender_field: str, scts: str, dcs: str = "00") -> str:
    """构造完整 SMS-DELIVER PDU（first octet=0x40 含 UDH）"""
    ud = bytes.fromhex(ud_hex)
    header_bits = 6 * 8  # 本例 UDH 固定 6 字节
    fill = (7 - header_bits % 7) % 7
    udl = (header_bits + fill) // 7 + (len(ud) * 8 - header_bits - fill) // 7
    return (
        "07917283559999F9"  # SMSC
        + "40"  # first octet: SMS-DELIVER + UDH
        + sender_field
        + "00"  # PID
        + dcs
        + scts
        + f"{udl:02X}"
        + ud_hex
    )


# "Lebara" 字母数字地址（TOA=D0，GSM7 打包）
SENDER_LEBARA = "0BD0CCB2382C0F03"
# "+447700900123" 国际号码地址（TOA=91，BCD 半字节交换: 44 77 00 09 10 32）
SENDER_INTL = "0C91447700091032"
# "38885" 短号码（BCD: 83 88 F5）
SENDER_SHORT = "05818388F5"


def test_decode_address_alphanumeric():
    assert decode_address(bytes.fromhex(SENDER_LEBARA)) == "Lebara"


def test_decode_address_international():
    assert decode_address(bytes.fromhex(SENDER_INTL)) == "+447700900123"


def test_decode_address_short():
    assert decode_address(bytes.fromhex("05818388F5")) == "38885"


def test_decode_scts():
    # 26/08/21 09:13:16 +08:00（+32 个 15 分钟）
    assert decode_scts(bytes.fromhex("62801290316123")) == "26/08/21, 09:13:16 +08"


def test_parse_concat_part1():
    pdu = build_deliver(UD_PART1, SENDER_LEBARA, "62801290316123")
    r = parse_deliver(pdu)
    assert r["number"] == "Lebara"
    assert r["part"] == "1/2"
    assert r["date"] == "26/08/21, 09:13:16 +08"
    assert r["text"].startswith(
        "Hello, WiFi calling and 4G calling (VoLTE calling) is now "
        "available on your Lebara mobile number."
    )


def test_parse_concat_part2():
    # 26/08/21 09:13:19 +08:00
    pdu = build_deliver(UD_PART2, SENDER_LEBARA, "62801290319123")
    r = parse_deliver(pdu)
    assert r["number"] == "Lebara"
    assert r["part"] == "2/2"
    assert r["date"] == "26/08/21, 09:13:19 +08"
    assert r["text"] == ".uk/en/Wificalling"


def test_parse_plain_gsm7():
    # 无 UDH 的普通 GSM7 短信：手工打包 "HI"
    # H=0x48, I=0x49 → octet0 = 0x48 | ((0x49 & 1) << 7) = 0xC8, octet1 = 0x49 >> 1 = 0x24
    pdu = (
        "07917283559999F9"  # SMSC
        + "00"  # first octet: SMS-DELIVER
        + SENDER_SHORT
        + "00"
        + "00"
        + "62801290316123"
        + "02"  # UDL = 2 septets
        + "C824"
    )
    r = parse_deliver(pdu)
    assert r["number"] == "38885"
    assert r["part"] is None
    assert r["text"] == "HI"


def test_parse_ucs2():
    # 中文 "你好" UCS-2 编码，无 UDH
    pdu = (
        "07917283559999F9"
        + "00"
        + SENDER_INTL
        + "00"
        + "08"  # DCS: UCS-2
        + "62801290316123"
        + "04"  # UDL = 4 字节
        + "4F60597D"
    )
    r = parse_deliver(pdu)
    assert r["text"] == "你好"


def make_part(index, number, date, text, seq, total, ref=0x28, status="REC READ"):
    """构造一条带级联信息的消息记录（模拟 _list_sms_pdu 的输出）"""
    return {
        "index": index,
        "status": status,
        "number": number,
        "date": date,
        "text": text,
        "part": f"{seq}/{total}",
        "_concat": {"ref": ref, "total": total, "seq": seq},
    }


def test_merge_concat_parts():
    plain = {
        "index": 1,
        "status": "REC READ",
        "number": "38885",
        "date": "26/08/20, 14:00:49 +00",
        "text": "Dear customer...",
        "part": None,
        "_concat": None,
    }
    # 故意乱序输入，且第 2 段时间更晚、状态未读
    p2 = make_part(3, "Lebara", "26/08/21, 09:13:16 +00", ".uk/en/Wificalling", 2, 2, status="REC UNREAD")
    p1 = make_part(2, "Lebara", "26/08/21, 09:13:13 +00", "https://www.lebara.co", 1, 2)

    merged = merge_concat_parts([plain, p2, p1])

    assert len(merged) == 2
    assert merged[0] is plain  # 普通短信原样保留
    m = merged[1]
    assert m["text"] == "https://www.lebara.co.uk/en/Wificalling"  # 按 seq 升序拼接
    assert m["index"] == 3  # 保留首条记录位置
    assert m["indexes"] == [2, 3]
    assert m["date"] == "26/08/21, 09:13:13 +00"  # 取最早时间
    assert m["status"] == "REC UNREAD"  # 任一分段未读则整体未读
    assert m["part"] is None  # 分段齐全
    assert "_concat" not in m and "_seq" not in m and "_total" not in m


def test_merge_concat_parts_incomplete():
    # 只收到第 2 段（第 1 段丢失）
    p2 = make_part(3, "Lebara", "26/08/21, 09:13:16 +00", ".uk/en/Wificalling", 2, 2)
    merged = merge_concat_parts([p2])
    assert len(merged) == 1
    assert merged[0]["text"] == ".uk/en/Wificalling"
    assert merged[0]["part"] == "已收1/2"
    assert merged[0]["indexes"] == [3]


def test_merge_concat_parts_different_sender():
    # 相同 ref 但发件人不同，不应合并
    a = make_part(5, "Alice", "26/08/21, 10:00:00 +00", "a", 1, 2)
    b = make_part(6, "Bob", "26/08/21, 10:00:01 +00", "b", 2, 2)
    merged = merge_concat_parts([a, b])
    assert len(merged) == 2
    assert merged[0]["text"] == "a"
    assert merged[1]["text"] == "b"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("\n全部通过")

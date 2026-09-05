#!/usr/bin/env python3
"""
ML307A Host VoWiFi 就绪性测试（VoWiFi/漫游数据激活前置验证）

背景：
Lebara 荷兰卡漫游在中国移动，蜂窝数据 PDP 激活但 TCP 全部被拒（错误码 559/580），
判断为运营商对漫游数据的限制。参考以下两个开源项目的实现思路：
- https://github.com/leonfox28/simplus （ML307A Host VoWiFi：SIM AKA + ePDG + Gm IPsec + IMS）
- https://github.com/MengMengCode/VoCat （Quectel 模组 Host VoWiFi：IKEv2 + EAP-AKA + IMS SMS）

实现原理（Host VoWiFi）：
ML307A 固件（ML307A-DSLN）不内置 VoWiFi/IMS，模块只作为「USIM 读卡器 + AKA 协处理器」：
1. 主机从 SIM 读 IMSI，推导 ePDG FQDN: epdg.epc.mnc<MNC>.mcc<MCC>.pub.3gppnetwork.org
   和 EAP-AKA 身份 NAI: <IMSI>@nai.epc.mnc<MNC>.mcc<MCC>.pub.3gppnetwork.org
2. 主机 IKEv2 连接运营商 ePDG（UDP 500/4500），ePDG 下发 EAP-AKA 挑战（RAND/AUTN）
3. 主机通过 AT+CSIM / AT+CGLA 把 RAND/AUTN 转成 USIM AUTHENTICATE APDU 交给 SIM 卡，
   取回 RES/CK/IK（AKA' 时为 RES/IK'/CK'），完成 EAP-AKA 应答
4. 建立 IPsec (XFRM) 隧道后经 Gm 接口向 P-CSCF 发起 SIP REGISTER（IMS 注册）
5. IMS 注册成功后即可收发 IMS SMS（SIP MESSAGE），不经蜂窝射频 —— SIMplus/VoCat
   即以此实现「无蜂窝信号也能收发短信」；部分运营商（如本卡的漫游限制）数据漫游
   被拦，但 VoWiFi 走本机宽带出口到 ePDG，不受漫游限制影响

本脚本实现可独立验证的分阶段测试：
  [阶段1] 模块与 SIM 就绪（AT/CPIN/CIMI/CSIM 能力）
  [阶段2] USIM 会话（EF_DIR→AID→ADF_USIM，读 EF_IMSI 验证，尝试 EF_IMPI/IMPU）
  [阶段3] SIM AKA 能力（AUTHENTICATE 假挑战 → 期待 AUTS 同步失败响应，证明卡支持 AKA）
  [阶段4] ePDG 发现（FQDN 推导 + 系统 DNS / Google DoH 兜底解析）
  [阶段5] ePDG IKE 可达性（UDP 500/4500 探测）
  [阶段6] 主机 VoWiFi 环境检查（XFRM/IPsec、TUN、strongSwan）
  [总结] 输出各项结论与完整 Host VoWiFi 的后续部署建议

用法：
.venv/bin/python tests/test_vowifi.py
.venv/bin/python tests/test_vowifi.py --port /dev/ttyUSB2 --skip-network
"""

import argparse
import json
import re
import socket
import sys
import time
import urllib.request

import serial

PORT = "/dev/ttyUSB2"
BAUDRATE = 115200

# 假挑战：随机 RAND + 全零 AUTN（SQN 不在卡内窗口 → USIM 应返回 AUTS 同步失败，
# 这恰好证明卡的 AKA 算法可被调用；MAC 错误时部分卡返回 6985/6984，同样证明命令路径可用）
DUMMY_RAND = "00112233445566778899aabbccddeeff"
DUMMY_AUTN = "00000000000000000000000000000000"

# 3GPP TS 31.102 AKA 响应 tag
TAG_RES = 0xA8   # 3G 成功: RES
TAG_CKIK = 0xDB  # 3G 成功: CK||IK (32字节)；2G 上下文为 RES+Kc
TAG_AKAP = 0xDC  # AKA' (4G) 成功: RES + IK'||CK'
TAG_AUTS = 0x4D  # 同步失败: AUTS (14字节)


def send_at(ser, cmd, wait=1.0, timeout=None):
    """发送 AT 指令并返回原始响应文本"""
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
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
                time.sleep(0.2)
                buf += ser.read(ser.in_waiting or 4096).decode(errors="ignore")
                break
    print(f"[调试] {cmd[:60]}{'...' if len(cmd) > 60 else ''} -> {buf.strip()[:160]!r}")
    return buf


# ---------------------------------------------------------------- APDU 层

def csim(ser, apdu_hex: str):
    """AT+CSIM 发送 APDU，返回 (响应数据hex, SW1SW2)"""
    resp = send_at(ser, f'AT+CSIM={len(apdu_hex)},"{apdu_hex}"', wait=1.5)
    m = re.search(r'\+CSIM:\s*(\d+),\s*"([0-9A-Fa-f]*)"', resp)
    if not m:
        # CME ERROR: 50 参数错 / 4 不支持等
        em = re.search(r"\+CME ERROR:\s*(\d+)", resp)
        raise RuntimeError(f"CSIM 失败: {resp.strip()!r} (CME {em.group(1) if em else '?'})")
    data = m.group(2).upper()
    sw = data[-4:]
    return data[:-4], sw


def ccho(ser, aid_hex: str):
    """AT+CCHO 打开逻辑通道，返回通道号。

    ML307A 返回裸数字（如 '2\r\n\r\nOK'）而非标准的 '+CCHO: 2'，两种都兼容。
    """
    resp = send_at(ser, f'AT+CCHO="{aid_hex}"', wait=2)
    m = re.search(r"\+CCHO:\s*(\d+)", resp) or re.search(r"^\s*([0-9]+)\s*$", resp.strip(), re.M)
    if not m:
        raise RuntimeError(f"CCHO 失败: {resp.strip()!r}")
    return int(m.group(1))


def cgla(ser, channel: int, apdu_hex: str):
    """AT+CGLA 逻辑通道发 APDU。

    apdu_hex 的 CLA 可传 00（由本函数替换为逻辑通道 CLA）。
    自动处理 SW=61xx/9Fxx 的 GET RESPONSE 跟随读取。
    返回 (数据hex, SW1SW2)。
    """
    apdu = f"{channel:02X}" + apdu_hex[2:]
    resp = send_at(ser, f'AT+CGLA={channel},{len(apdu)},"{apdu}"', wait=3)
    m = re.search(r'\+CGLA:\s*\d+,\s*"([0-9A-Fa-f]*)"', resp)
    if not m:
        raise RuntimeError(f"CGLA 失败: {resp.strip()!r}")
    data = m.group(1).upper()
    sw = data[-4:]
    body = data[:-4]
    if sw[:2] in ("61", "9F"):  # 需 GET RESPONSE 取回数据
        le = sw[2:]
        resp2 = send_at(ser, f'AT+CGLA={channel},10,"{channel:02X}C00000{le}"', wait=2)
        m2 = re.search(r'\+CGLA:\s*\d+,\s*"([0-9A-Fa-f]*)"', resp2)
        if m2:
            d2 = m2.group(1).upper()
            body += d2[:-4]
            sw = d2[-4:]
    return body, sw


def sw_ok(sw: str) -> bool:
    return sw == "9000" or sw[:2] == "91"


def parse_tlv(data: bytes, tag: int):
    """简单 TLV 遍历（含一层嵌套模板），返回所有指定 tag 的 value"""
    out = []

    def walk(buf):
        i = 0
        while i + 2 <= len(buf):
            t = buf[i]
            if t in (0x00, 0xFF):
                i += 1
                continue
            ln = buf[i + 1]
            val = buf[i + 2 : i + 2 + ln]
            if t == tag:
                out.append(val)
            if t & 0x20 or t in (0x61, 0x6F):  # 模板则递归
                walk(val)
            i += 2 + ln
        return

    walk(data)
    return out


def select_by_fid(apdu_fn, fid: str):
    """SELECT by file id（P2=0C 无 FCP 返回，兼容性最好）"""
    return apdu_fn(f"00A4000C02{fid}")


def select_by_aid(apdu_fn, aid: str):
    """SELECT application by AID"""
    return apdu_fn(f"00A4040C{len(aid) // 2:02X}{aid}")


def read_binary(apdu_fn, length=255, offset=0):
    return apdu_fn(f"00B0{offset:04X}{min(length, 255):02X}")


def read_record(apdu_fn, record=1, length=255):
    """READ RECORD（EF_DIR 等 linear/TLV 记录文件用，READ BINARY 会报 6981）"""
    return apdu_fn(f"00B2{record:02X}04{min(length, 255):02X}")


def decode_ef_imsi(data: bytes) -> str:
    """EF_IMSI 内容 → IMSI 字符串（半字节反转）"""
    n_digits = data[0] & 0x7F
    digits = []
    for b in data[1:]:
        digits.append(str(b & 0xF))
        digits.append(str((b >> 4) & 0xF))
    return "".join(digits)[:n_digits]


# ---------------------------------------------------------------- 阶段实现

def stage1_modem_ready(ser):
    """阶段1: 模块与 SIM 就绪"""
    print("\n========== [阶段1] 模块与 SIM 就绪检查 ==========")
    if "OK" not in send_at(ser, "AT"):
        print("✗ 模块无响应")
        return None
    send_at(ser, "AT+CMEE=1")
    resp = send_at(ser, "AT+CPIN?")
    if "READY" not in resp:
        print(f"✗ SIM 未就绪: {resp.strip()}")
        return None
    imsi = ""
    for ln in send_at(ser, "AT+CIMI").splitlines():
        ln = ln.strip()
        if ln.isdigit() and len(ln) in (14, 15):
            imsi = ln
            break
    if not imsi:
        print("✗ 读取 IMSI 失败")
        return None
    # CSIM/CCHO/CGLA 能力（simplus 的 ML307A 实测能力集）
    caps = {}
    for cmd, name in [("AT+CSIM=?", "CSIM"), ("AT+CCHO=?", "CCHO"), ("AT+CGLA=?", "CGLA")]:
        caps[name] = "OK" in send_at(ser, cmd)
    print(f"✓ IMSI: {imsi}")
    print(f"✓ 指令能力: " + ", ".join(f"{k}={'支持' if v else '不支持'}" for k, v in caps.items()))
    if not caps["CSIM"] and not (caps["CCHO"] and caps["CGLA"]):
        print("✗ 模块无 APDU 通道（CSIM/CGLA 均不可用），无法做 Host VoWiFi 的 SIM AKA")
        return None
    return {"imsi": imsi, "caps": caps}


def stage2_usim_session(ser, caps):
    """阶段2: USIM 会话建立（EF_DIR → AID → ADF_USIM → EF 验证）"""
    print("\n========== [阶段2] USIM 会话建立 ==========")
    # 主通道走 CSIM；CGLA 逻辑通道备用
    apdu_fn = lambda h: csim(ser, h)
    try:
        # MF → EF_DIR(2F00) → 解析 ADF_USIM AID
        select_by_fid(apdu_fn, "3F00")
        sw_d, sw = select_by_fid(apdu_fn, "2F00")
        if not sw_ok(sw):
            print(f"✗ SELECT EF_DIR 失败 SW={sw}")
            return None
        data, sw = read_binary(apdu_fn, 255)
        if not sw_ok(sw):
            # EF_DIR 是记录文件，READ BINARY 会返回 6981（结构不符），改用 READ RECORD
            data, sw = read_record(apdu_fn, 1)
            if sw_ok(sw):
                # 循环读后续记录直到记录不存在（6A83/6283）
                rec = 2
                while sw_ok(sw) and rec <= 10:
                    d2, sw2 = read_record(apdu_fn, rec)
                    if sw_ok(sw2) and d2:
                        data += d2
                        sw = sw2
                    rec += 1
                    if sw2[:2] in ("6A", "62"):
                        break
        if not sw_ok(sw):
            print(f"✗ 读取 EF_DIR 失败 SW={sw}")
            return None
        aids = [a.hex().upper() for a in parse_tlv(bytes.fromhex(data), 0x4F)]
        # USIM 应用 AID 前缀 A0000000871002
        usim_aids = [a for a in aids if a.startswith("A0000000871002")] or aids
        if not usim_aids:
            print(f"✗ EF_DIR 中未找到应用 AID: {data[:80]}...")
            return None
        aid = usim_aids[0]
        print(f"✓ ADF_USIM AID: {aid}")

        # SELECT ADF_USIM → 读 EF_IMSI 验证
        _, sw = select_by_aid(apdu_fn, aid)
        if not sw_ok(sw):
            # CSIM 主通道失败则切换 CGLA 逻辑通道
            if caps.get("CCHO") and caps.get("CGLA"):
                print("! CSIM 主通道选择失败，改用 CGLA 逻辑通道")
                ch = ccho(ser, aid)
                apdu_fn = lambda h: cgla(ser, ch, h)
                _, sw = select_by_aid(apdu_fn, aid)
                if not sw_ok(sw):
                    print(f"✗ 逻辑通道 SELECT AID 失败 SW={sw}")
                    return None
            else:
                print(f"✗ SELECT ADF_USIM 失败 SW={sw}")
                return None
        _, sw = select_by_fid(apdu_fn, "6F07")
        data, sw = read_binary(apdu_fn, 9)
        if sw_ok(sw) and data:
            imsi_ef = decode_ef_imsi(bytes.fromhex(data))
            # 与 AT+CIMI 权威值交叉校验；部分卡 EF 数据带非标准前导，取后缀匹配
            imsi_at = None
            m = re.search(r"\d{14,15}", send_at(ser, "AT+CIMI"))
            if m:
                imsi_at = m.group(0)
            if imsi_at and imsi_at in imsi_ef:
                print(f"✓ EF_IMSI 读取成功并与 AT+CIMI 一致: {imsi_at}")
            else:
                print(f"! EF_IMSI={imsi_ef} 与 AT+CIMI={imsi_at} 不完全一致（非标准卡数据，不影响 AKA）")
        else:
            print(f"! EF_IMSI 读取失败 SW={sw}（不影响 AKA 测试）")
            imsi_ef = None

        # IMS 身份（很多卡不预置，容错处理）
        impi = impu = None
        for fid, name in [("6F02", "EF_IMPI"), ("6F03", "EF_IMPU")]:
            try:
                _, sw = select_by_fid(apdu_fn, fid)
                if not sw_ok(sw):
                    continue  # 文件不存在（6A82），跳过且不发 READ（避免误读当前文件）
                data, sw = read_binary(apdu_fn, 64)
                if sw_ok(sw) and data:
                    val = bytes.fromhex(data).decode("utf-16-be", errors="ignore")
                    # EF_IMPI 是 UTF-16STR/八位组串，尝试两种解码
                    if not val.strip("\x00"):
                        val = bytes.fromhex(data).decode("ascii", errors="ignore")
                    val = val.strip("\x00").strip()
                    if val:
                        if name == "EF_IMPI":
                            impi = val
                        else:
                            impu = val
                        print(f"✓ {name}: {val}")
            except Exception:
                pass

        return {"aid": aid, "apdu_fn": apdu_fn, "imsi_ef": imsi_ef, "impi": impi, "impu": impu}
    except Exception as e:
        print(f"✗ USIM 会话失败: {e}")
        return None


def stage3_sim_aka(ser, session):
    """阶段3: SIM AKA 能力验证（AUTHENTICATE 假挑战）

    APDU 格式与 simplus 实现完全一致（ml307a_simaka.go buildUSIMAKAAPDU）：
        00 88 00 81 22 10<RAND 16字节> 10<AUTN 16字节>
    P2=0x81 为 AKA'（EPS/EAP-AKA'，ePDG 场景标准），Lc=0x22（34=1+16+1+16）。
    必须经 CGLA 逻辑通道发送（实测 CSIM 主通道返回 6A86）。
    假 AUTN 的 SQN 必然不在卡内窗口，预期返回 9862（同步失败）。
    """
    print("\n========== [阶段3] SIM AKA 能力验证 ==========")
    rand, autn = DUMMY_RAND, DUMMY_AUTN
    try:
        # 重新打开逻辑通道（stage2 可能用的是 CSIM 主通道）
        ch = ccho(ser, session["aid"])
        try:
            data, sw = cgla(ser, ch, f"008800812210{rand}10{autn}")
        finally:
            send_at(ser, f"AT+CCHC={ch}", wait=0.5)
    except Exception as e:
        print(f"✗ AUTHENTICATE 发送失败: {e}")
        return False

    print(f"原始响应: data={data or '(空)'} SW={sw}")
    if sw == "9862":
        print("✓ SW=9862 AKA 同步失败 —— 假挑战的 SQN 不在卡内窗口，这是预期结果，")
        print("  证明 SIM 的 AKA' 算法（AUTHENTICATE）可被正常调用，EAP-AKA' 鉴权链路可用；")
        print("  真实场景中 ePDG 下发的挑战 SQN 有效，会返回 RES/CK/IK 完成鉴权")
        return True
    if sw_ok(sw) and data:
        raw = bytes.fromhex(data)
        auts_list = parse_tlv(raw, TAG_AUTS)
        if auts_list:
            print(f"✓ USIM 返回 AUTS 同步失败（tag 4D, {len(auts_list[0])} 字节）——AKA 可用")
            return True
        if parse_tlv(raw, TAG_RES) or parse_tlv(raw, TAG_CKIK) or parse_tlv(raw, TAG_AKAP):
            print("✓ USIM 返回了鉴权向量（RES/CK/IK）——AKA 完全可用")
            return True
        print(f"? 成功响应但含未识别结构: {data}")
        return False
    # 6985=使用条件不满足 6984=数据无效 6982=安全状态不满足 6A88=引用数据未找到
    if sw in ("6985", "6984", "6982", "6A88"):
        print(f"! SW={sw}：命令路径可用但被卡拒绝（需先完成应用选择/安全状态）")
        print("  完整实现中 strongSwan 收到 ePDG 真实挑战后按同样 APDU 转发即可")
        return False
    print(f"✗ 非预期 SW={sw}，USIM 不支持 AUTHENTICATE 或应用上下文错误")
    return False


def mcc_mnc_candidates(imsi: str):
    """由 IMSI 推导 (MCC, MNC) 候选：2 位与 3 位 MNC 两种解释"""
    mcc = imsi[:3]
    return [(mcc, imsi[3:5]), (mcc, imsi[3:6])]


def epdg_fqdn(mcc: str, mnc: str) -> str:
    return f"epdg.epc.mnc{int(mnc):03d}.mcc{mcc}.pub.3gppnetwork.org"


def nai(imsi: str, mcc: str, mnc: str) -> str:
    return f"{imsi}@nai.epc.mnc{int(mnc):03d}.mcc{mcc}.pub.3gppnetwork.org"


def doh_resolve(host: str):
    """Google DoH 兜底解析（ePDG 域名常按地理分流，海外 DoH 结果可能更接近真实部署）"""
    url = f"https://dns.google/resolve?name={host}&type=A"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            ans = json.load(resp).get("Answer", [])
            return [a["data"] for a in ans if a.get("type") == 1]
    except Exception as e:
        print(f"  DoH 解析失败: {e}")
        return []


def stage4_epdg_discovery(imsi: str, skip_network=False):
    """阶段4: ePDG 发现（FQDN 推导 + DNS 解析）"""
    print("\n========== [阶段4] ePDG 发现 ==========")
    found = None
    for mcc, mnc in mcc_mnc_candidates(imsi):
        fqdn = epdg_fqdn(mcc, mnc)
        print(f"候选 MCC={mcc} MNC={mnc} → {fqdn}")
        print(f"  EAP-AKA NAI: {nai(imsi, mcc, mnc)}")
        if skip_network:
            continue
        ips = []
        try:
            ips = sorted({ai[4][0] for ai in socket.getaddrinfo(fqdn, None, socket.AF_INET)})
        except socket.gaierror:
            pass
        if not ips:
            ips = doh_resolve(fqdn)
            if ips:
                print(f"  (系统 DNS 失败，Google DoH 解析成功)")
        # 过滤回环/私有地址：不存在的 3GPP 域名会被某些 DNS 解析为 127.0.0.1 等垃圾结果
        ips = [ip for ip in ips if not ip.startswith(("127.", "0.", "10.", "192.168.", "172.")) and ip != "255.255.255.255"]
        if ips:
            print(f"  ✓ 解析成功: {', '.join(ips)}")
            found = found or {"mcc": mcc, "mnc": mnc, "fqdn": fqdn, "ips": ips}
        else:
            print(f"  - 解析失败（该 MNC 解释不成立或未部署 ePDG）")
    if skip_network:
        print("! 已跳过网络测试（--skip-network）")
        return None
    if not found:
        print("✗ 所有 ePDG 候选域名均解析失败（运营商可能未部署 ePDG 或 DNS 受限）")
    return found


def stage5_epdg_reachability(epdg):
    """阶段5: ePDG IKE 可达性探测（UDP 500/4500）"""
    print("\n========== [阶段5] ePDG IKE 可达性 ==========")
    if not epdg:
        print("! 无 ePDG 地址，跳过")
        return
    for ip in epdg["ips"][:2]:
        for port in (500, 4500):
            # 弱探测：发最小 IKE 头格式（28字节）垃圾，等待任何回包
            # ePDG 可能不回垃圾包，无响应 ≠ 不可达，仅作参考
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(3)
                s.connect((ip, port))
                # 伪造 IKE 头: initiator SPI 8 + responder SPI 8 + flags
                s.send(bytes.fromhex("0123456789abcdef" + "0" * 16 + "212020000000000000002200"))
                try:
                    s.recv(512)
                    print(f"✓ {ip}:{port} 有响应（ePDG 可达，IKE 服务在运行）")
                except socket.timeout:
                    print(f"- {ip}:{port} 无响应（可能是探测包被静默丢弃，不代表不可达）")
                s.close()
            except OSError as e:
                print(f"✗ {ip}:{port} 发送失败: {e}")


def stage6_host_env():
    """阶段6: 主机 VoWiFi 环境检查"""
    import os
    import shutil
    import subprocess

    print("\n========== [阶段6] 主机 VoWiFi 环境检查 ==========")

    def xfrm_functional_check():
        """功能性实测 XFRM：添加/删除一条 ESP state。

        注意：不能用 /proc/net/xfrm 是否存在来判断（该 proc 项依赖
        CONFIG_XFRM_STATISTICS，LXC 等环境常不暴露），直接调 ip 命令才可靠。
        """
        if shutil.which("ip") is None:
            return False
        spi = "0xdead1234"
        add = (
            f"ip xfrm state add src 192.0.2.1 dst 192.0.2.2 "
            f"proto esp spi {spi} mode tunnel "
            f"auth sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef "
            f"enc aes 0123456789abcdef0123456789abcdef"
        )
        try:
            r = subprocess.run(add, shell=True, capture_output=True, timeout=5)
            if r.returncode != 0:
                return False
            subprocess.run(
                f"ip xfrm state delete src 192.0.2.1 dst 192.0.2.2 proto esp spi {spi}",
                shell=True, capture_output=True, timeout=5,
            )
            return True
        except Exception:
            return False

    checks = []
    checks.append(("内核 XFRM/IPsec（实测 ESP state 添加/删除）", xfrm_functional_check()))
    checks.append(("TUN 设备 (/dev/net/tun)", os.path.exists("/dev/net/tun")))
    checks.append(("strongSwan (ipsec)", shutil.which("ipsec") is not None))
    checks.append(("ip 命令", shutil.which("ip") is not None))
    for name, ok in checks:
        print(f"{'✓' if ok else '✗'} {name}: {'就绪' if ok else '缺失'}")
    if not checks[0][1]:
        print("  ! XFRM 不可用常见于：内核未启用 CONFIG_XFRM、或容器缺少 NET_ADMIN 权限")
    print("""
说明：完整 Host VoWiFi 需要内核 XFRM 支持（IKEv2 IPsec 隧道）与 TUN 设备。
两种部署路径：
  A) strongSwan + EAP-AKA/simaka 插件（simplus 方案）：
     自定义 eap 插件在收到 EAP-AKA 挑战时，经串口把 RAND/AUTN 转发给 ML307A 上的
     SIM 卡（本脚本阶段3的 APDU 流程），取回 RES/CK/IK 完成应答；
     Ubuntu/Debian 可直接 apt install strongswan（含 eap-aka/aka2 插件）
  B) 用户态 IKE 栈（VoCat / eapsim 方案）：
     纯 Go/Python 实现 IKEv2 + EAP-AKA + ESP + XFRM 安装，模组只做 APDU 通道。
IPsec 隧道建立后：向 P-CSCF 发 SIP REGISTER（IMS 注册），随后经 SIP MESSAGE 收发
IMS 短信 —— 短信流量走本机宽带 → ePDG → 运营商 IMS，不占用蜂窝漫游数据。""")


def main():
    parser = argparse.ArgumentParser(description="ML307A Host VoWiFi 就绪性测试")
    parser.add_argument("--port", default=PORT)
    parser.add_argument("--baudrate", type=int, default=BAUDRATE)
    parser.add_argument("--skip-network", action="store_true", help="跳过 ePDG DNS/UDP 探测")
    args = parser.parse_args()

    try:
        ser = serial.Serial(args.port, args.baudrate, timeout=1)
    except serial.SerialException as e:
        print(f"打开串口失败: {e}")
        return 1

    results = {}
    try:
        s1 = stage1_modem_ready(ser)
        results["模块就绪"] = bool(s1)
        if not s1:
            return 1
        session = stage2_usim_session(ser, s1["caps"])
        results["USIM 会话"] = bool(session)
        if session:
            results["SIM AKA"] = stage3_sim_aka(ser, session)
        epdg = stage4_epdg_discovery(s1["imsi"], args.skip_network)
        results["ePDG 发现"] = bool(epdg)
        stage5_epdg_reachability(epdg)
        stage6_host_env()
    finally:
        ser.close()

    print("\n========== 总结 ==========")
    for k, v in results.items():
        print(f"{'✓' if v else '✗'} {k}")
    if all(results.values()):
        print("\n所有前置条件就绪：可继续部署完整 Host VoWiFi（IKEv2+EAP-AKA+IMS）")
        return 0
    print("\n部分前置条件缺失，按上文各阶段输出排查。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

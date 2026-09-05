#!/usr/bin/env python3
"""ike_probe.py: 对 ePDG 发送最小 IKE_SA_INIT 探测 UDP 500 可达性。

KE 载荷用随机 256 字节（任何小于素数的值都是合法的 modp2048 DH 公钥），
响应方必然回包（NO_PROPOSAL_CHOSEN / COOKIE / SA_INIT 响应），任意响应
即证明该 ePDG 的 IKE 可达。用于诊断 ISP/GFW 对 IPsec 的针对性丢弃。
"""
import os
import socket
import struct
import sys


def payload(ptype, data):
    return struct.pack(">BBH", 0, ptype, len(data) + 4) + data


def build_sa_init():
    # --- SA payload: 1 proposal, IKE ---
    def transform(ttype, tid, keylen=None):
        t = struct.pack(">BBH", ttype, 0, tid)
        if keylen:
            t += struct.pack(">BBH", 0x80, 0x0E, keylen)
        return t

    transforms = b""
    for tid, keylens in ((12, (256, 128)),):  # ENCR_AES_CBC
        for kl in keylens:
            transforms += transform(1, tid, kl)
    for tid in (12, 2):  # PRF: HMAC_SHA2_256, HMAC_SHA1
        transforms += transform(2, tid)
    for tid in (12, 2):  # INTEG: SHA2_256_128, SHA1_96
        transforms += transform(3, tid)
    transforms += transform(4, 14)  # DH MODP_2048

    proposal = struct.pack(">BBHBBBB", 1, 0, 8 + len(transforms), 1, 0, 0, 4) + transforms
    sa = payload(33, proposal)

    # --- KE payload: MODP_2048, 随机公钥 ---
    ke = payload(34, struct.pack(">HH", 14, 0) + os.urandom(256))

    # --- Nonce ---
    ni = payload(40, os.urandom(32))

    return sa + ke + ni


def probe(host, port=500, timeout=5):
    ispi = os.urandom(8)
    body = build_sa_init()
    header = (
        ispi
        + b"\x00" * 8
        + bytes([33, 0x20, 0x08, 0, 0, 0, 0, 0])  # NP=SA, v2.0, INIT flag
        + struct.pack(">II", 0, 28 + len(body))
    )
    pkt = header + body

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (host, port))
        data, addr = s.recvfrom(4096)
        return len(data), data[16], data[18] if len(data) > 18 else 0
    except socket.timeout:
        return None, None, None
    finally:
        s.close()


def main():
    targets = sys.argv[1:] or ["109.39.144.148"]
    for t in targets:
        n, ver, flags = probe(t)
        if n:
            print(f"{t:20s} 可达   响应 {n} 字节 (发起者视角 flags=0x{flags:02x})")
        else:
            print(f"{t:20s} 无响应")


if __name__ == "__main__":
    main()

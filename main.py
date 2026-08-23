import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from at_serial import ATError, ATSerial
from sms_pdu import PDUParseError, merge_concat_parts, parse_deliver

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

PORT = os.getenv("SMS_PORT", "/dev/ttyUSB2")
BAUD = int(os.getenv("SMS_BAUD", "115200"))

modem = ATSerial(port=PORT, baudrate=BAUD)

modem_state: dict = {
    "connected": False,
    "updated_at": None,
    "module": {},
    "signal": {},
    "sim": {},
    "device": {},
}

sms_cache: list[dict] = []
sms_cache_lock = asyncio.Lock()

app = FastAPI(title="SMS 网关")

keepalive_config = {
    "enabled": False,
    "mode": "url",
    "url": "http://www.baidu.com",
    "sms_number": "10086",
    "sms_text": "666",
    "interval_hours": 24,
    "last_run": None,
    "last_result": None,
}
keepalive_lock = asyncio.Lock()


async def _query_cached_csq():
    if not modem.connected:
        return {}
    try:
        r = await modem.send_command("AT+CSQ", timeout=3)
        for line in r:
            if "+CSQ:" in line:
                parts = line.split(":")[1].strip().split(",")
                csq = int(parts[0])
                return {
                    "csq": csq,
                    "rssi": -113 + 2 * csq if 0 <= csq <= 31 else None,
                    "ber": int(parts[1]) if len(parts) > 1 and parts[1] != "99" else None,
                }
    except Exception as e:
        logger.debug("CSQ query failed: %s", e)
    return {}


async def _query_extended_signal():
    if not modem.connected:
        return {}
    try:
        r = await modem.send_command("AT+CESQ", timeout=3)
        for line in r:
            if "+CESQ:" in line:
                parts = line.split(":")[1].strip().split(",")
                v = [int(p) if p not in ("99", "255") else None for p in parts]
                return {
                    "rxlev": v[0] if len(v) > 0 else None,
                    "rscp": v[2] if len(v) > 2 else None,
                    "ecno": v[3] if len(v) > 3 else None,
                    "rsrq": v[4] if len(v) > 4 else None,
                    "rsrp": v[5] if len(v) > 5 else None,
                }
    except Exception as e:
        logger.debug("CESQ query failed: %s", e)
    return {}


async def _query_module_status():
    if not modem.connected:
        return {}
    result = {}
    try:
        r = await modem.send_command("AT", timeout=2)
        result["at"] = "ok"
    except Exception:
        result["at"] = "error"

    try:
        r = await modem.send_command("AT+CPIN?", timeout=3)
        for line in r:
            if "+CPIN:" in line:
                result["sim"] = line.split(":")[1].strip()
    except Exception:
        result["sim"] = "error"

    try:
        r = await modem.send_command("AT+CREG?", timeout=3)
        for line in r:
            if "+CREG:" in line:
                m = re.search(r"\+CREG:\s*(\d),(\d)", line)
                if m:
                    codes = {0: "未注册", 1: "已注册(本地)", 2: "搜索中", 3: "拒绝", 4: "未知", 5: "已注册(漫游)"}
                    result["network"] = codes.get(int(m.group(2)), m.group(2))
    except Exception:
        result["network"] = "error"

    try:
        r = await modem.send_command("AT+CGATT?", timeout=3)
        for line in r:
            if "+CGATT:" in line:
                result["data_attached"] = "1" in line.split(":")[1]
    except Exception:
        result["data_attached"] = False

    return result


async def _query_sim_info():
    if not modem.connected:
        return {}
    result = {}
    try:
        r = await modem.send_command("AT+COPS?", timeout=3)
        for line in r:
            if "+COPS:" in line:
                ops = line.split(":")[1].strip()
                parts = [x.strip().strip('"') for x in ops.split(",")]
                result["operator"] = parts[-1] if parts else "unknown"
    except Exception:
        pass
    try:
        r = await modem.send_command("AT+CNUM", timeout=3)
        for line in r:
            if "+CNUM:" in line:
                nums = line.split('"')
                if len(nums) > 1:
                    result["number"] = nums[1]
    except Exception:
        pass
    try:
        r = await modem.send_command("AT+CGSN", timeout=3)
        for line in r:
            if line and not line.startswith("AT") and not line.startswith("+") and len(line) > 10:
                result["imei"] = line.strip()
    except Exception:
        pass
    try:
        r = await modem.send_command("AT+CCID", timeout=3)
        for line in r:
            if "+CCID:" in line:
                result["iccid"] = line.split(":")[1].strip()
            elif line and not line.startswith("AT") and len(line) > 10:
                result["iccid"] = line.strip()
    except Exception:
        pass
    try:
        r = await modem.send_command("AT+CIMI", timeout=3)
        for line in r:
            if line and not line.startswith("AT") and not line.startswith("+") and len(line) > 5:
                result["imsi"] = line.strip()
    except Exception:
        pass
    try:
        r = await modem.send_command("AT+CGDCONT?", timeout=3)
        for line in r:
            if "+CGDCONT:" in line:
                parts = line.split(",")
                if len(parts) > 2:
                    apn = parts[2].strip().strip('"')
                    if apn:
                        result["apn"] = apn
    except Exception:
        pass
    try:
        r = await modem.send_command("AT+CGPADDR", timeout=3)
        for line in r:
            if "+CGPADDR:" in line:
                ip_parts = line.split(",")
                if len(ip_parts) > 1:
                    result["ip"] = ip_parts[1].strip().strip('"')
    except Exception:
        pass
    return result


async def _query_device_info():
    if not modem.connected:
        return {}
    result = {}
    for cmd, key in [("AT+CGMI", "manufacturer"), ("AT+CGMM", "model"), ("AT+CGMR", "revision")]:
        try:
            r = await modem.send_command(cmd, timeout=3)
            for line in r:
                if line and not line.startswith("AT") and not line.startswith("+") and not line.startswith("OK"):
                    result[key] = line.strip()
        except Exception:
            pass
    return result


async def _poll_modem_state():
    while True:
        await asyncio.sleep(8)
        if not modem.connected:
            continue
        try:
            module, signal_simple, signal_ext, sim, device = await asyncio.gather(
                _query_module_status(),
                _query_cached_csq(),
                _query_extended_signal(),
                _query_sim_info(),
                _query_device_info(),
                return_exceptions=True,
            )
            if isinstance(module, Exception):
                module = {"at": "error"}
            signal = signal_simple if not isinstance(signal_simple, Exception) else {}
            if not isinstance(signal_ext, Exception):
                signal.update(signal_ext)
            if isinstance(sim, Exception):
                sim = {}
            if isinstance(device, Exception):
                device = {}

            global modem_state
            modem_state = {
                "connected": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "module": module,
                "signal": signal,
                "sim": sim,
                "device": device,
            }
            logger.debug("Modem state refreshed")
        except Exception as e:
            logger.error("State poll error: %s", e)


async def _refresh_sms_cache():
    while True:
        await asyncio.sleep(15)
        if not modem.connected:
            continue
        try:
            msgs = await _list_sms()
            async with sms_cache_lock:
                sms_cache.clear()
                sms_cache.extend(msgs)
        except Exception as e:
            logger.debug("SMS refresh error: %s", e)


def _decode_ucs2(text: str) -> str:
    s = text.strip()
    if not s or len(s) < 4 or len(s) % 4 != 0:
        return text
    if not re.fullmatch(r"[0-9A-Fa-f]+", s):
        return text
    try:
        raw = bytes.fromhex(s)
        decoded = raw.decode("utf-16-be")
        if not decoded.isprintable():
            return text
        # 纯 ASCII 结果可能是巧合（如数字串），仅在包含非 ASCII 时才替换
        if decoded.isascii():
            return text
        return decoded
    except (ValueError, UnicodeDecodeError):
        return text


async def _list_sms_pdu() -> list[dict]:
    """PDU 模式（AT+CMGF=0）列出短信并自行解析。

    文本模式无法正确呈现带 UDH 的长短信（会输出为十六进制串）
    以及字母数字发件人地址，因此优先使用 PDU 模式。
    长短信各分段按「发件人+级联ref」合并为一条返回。
    """
    await modem.send_command("AT+CMGF=0", timeout=2)
    try:
        r = await modem.send_command("AT+CMGL=4", timeout=15)
    finally:
        await modem.send_command("AT+CMGF=1", timeout=2)

    stat_map = {0: "REC UNREAD", 1: "REC READ", 2: "STO UNSENT", 3: "STO SENT"}
    messages: list[dict] = []
    i = 0
    while i < len(r):
        m = re.match(r'\+CMGL:\s*(\d+)\s*,\s*(\d+|"[^"]*")', r[i])
        if not m:
            i += 1
            continue
        idx = int(m.group(1))
        stat_raw = m.group(2).strip('"')
        status = stat_map.get(int(stat_raw), stat_raw) if stat_raw.isdigit() else stat_raw

        # PDU 位于 +CMGL 行之后的下一非空行
        i += 1
        while i < len(r) and not r[i].strip():
            i += 1
        if i >= len(r):
            break
        pdu = r[i].strip()
        i += 1

        try:
            parsed = parse_deliver(pdu)
        except PDUParseError as e:
            # 状态报告等非 DELIVER 报文跳过；解析失败保留原始 PDU 供排查
            logger.debug("PDU 解析失败 (index=%s): %s", idx, e)
            messages.append(
                {
                    "index": idx,
                    "status": status,
                    "number": "",
                    "date": "",
                    "text": pdu,
                    "part": None,
                }
            )
            continue

        messages.append(
            {
                "index": idx,
                "status": status,
                "number": parsed["number"],
                "date": parsed["date"],
                "text": parsed["text"],
                "part": parsed["part"],
                "_concat": parsed["concat"],
            }
        )

    return merge_concat_parts(messages)


async def _list_sms() -> list[dict]:
    if not modem.connected:
        return []

    # 优先 PDU 模式（长短信/字母发件人在文本模式下会乱码）
    try:
        return await _list_sms_pdu()
    except Exception as e:
        logger.warning("PDU 模式列出短信失败，回退文本模式: %s", e)

    # 回退：文本模式（旧逻辑）
    try:
        await modem.send_command("AT+CMGF=1", timeout=2)
    except ATError:
        return []

    try:
        r = await modem.send_command('AT+CMGL="ALL"', timeout=10)
    except ATError:
        return []

    messages: list[dict] = []
    i = 0
    while i < len(r):
        line = r[i]
        # 标准格式：带引号的号码
        m = re.match(r'\+CMGL:\s*(\d+),"(.*?)","(.*?)"(?:,*)?,"(.*?)"', line)
        if m:
            idx = int(m.group(1))
            status = m.group(2)
            number_raw = m.group(3)
            date_raw = m.group(4)
            number = _decode_ucs2(number_raw)

            text_parts: list[str] = []
            i += 1
            while i < len(r) and not r[i].startswith("+CMGL:") and r[i] != "":
                text_parts.append(r[i])
                i += 1
            raw_text = "\n".join(text_parts).strip()
            text = _decode_ucs2(raw_text)

            parsed_date = date_raw.replace("+", " ").strip()
            messages.append(
                {
                    "index": idx,
                    "status": status,
                    "number": number,
                    "date": parsed_date,
                    "text": text,
                    "part": None,
                }
            )
            continue

        # 部分调制解调器输出未加引号的 UCS-2 编码号码
        m2 = re.match(r'\+CMGL:\s*(\d+),"(.*?)",([0-9+*#?]+)(?:,*)?,"(.*?)"', line)
        if m2:
            idx = int(m2.group(1))
            status = m2.group(2)
            number_raw = m2.group(3)
            date_raw = m2.group(4)
            number = _decode_ucs2(number_raw)

            text_parts: list[str] = []
            i += 1
            while i < len(r) and not r[i].startswith("+CMGL:") and r[i] != "":
                text_parts.append(r[i])
                i += 1
            raw_text = "\n".join(text_parts).strip()
            text = _decode_ucs2(raw_text)

            parsed_date = date_raw.replace("+", " ").strip()
            messages.append(
                {
                    "index": idx,
                    "status": status,
                    "number": number,
                    "date": parsed_date,
                    "text": text,
                    "part": None,
                }
            )
            continue

        i += 1

    messages.sort(key=lambda x: x["index"], reverse=True)
    return messages


async def _keepalive_execute(url: str) -> str:
    try:
        await modem.send_command("AT+CGATT=1", timeout=5)
        await asyncio.sleep(1)
        resp = await modem.send_command("AT+CGACT=1,1", timeout=10)
        ip = ""
        for line in resp:
            if "+CGACT:" in line and ",1" in line:
                ip = "ok"
        resp2 = await modem.send_command("AT+CGPADDR=1", timeout=5)
        for line in resp2:
            if "+CGPADDR:" in line:
                ip = line.split(",")[-1].strip().strip('"')

        await modem.send_command("AT+HTTPINIT", timeout=5)
        await modem.send_command('AT+HTTPPARA="CID",1', timeout=5)
        await modem.send_command(f'AT+HTTPPARA="URL","{url}"', timeout=5)
        await modem.send_command("AT+HTTPACTION=0", timeout=30)

        result = await modem.send_command("AT+HTTPREAD", timeout=10)
        body = "\n".join(result) if result else "(empty)"

        await modem.send_command("AT+HTTPTERM", timeout=5)
        logger.info("Keep-alive OK: %s (IP: %s)", url, ip)
        return f"OK | IP: {ip} | 响应长度: {len(body)}"
    except ATError as e:
        logger.warning("Keep-alive AT error: %s", e)
        return f"AT失败: {e}"
    except Exception as e:
        logger.warning("Keep-alive error: %s", e)
        return f"异常: {e}"


async def _keepalive_execute_sms(number: str, text: str) -> str:
    try:
        await modem.send_command("AT+CMGF=1", timeout=2)
        ref = await modem.send_sms(number, text, timeout=60)
        logger.info("Keep-alive SMS OK -> %s (ref: %s)", number, ref)
        return f"OK | SMS 发送成功, 引用号: {ref}"
    except ATError as e:
        logger.warning("Keep-alive SMS AT error: %s", e)
        return f"SMS失败: {e}"
    except Exception as e:
        logger.warning("Keep-alive SMS error: %s", e)
        return f"异常: {e}"


async def _keepalive_loop():
    while True:
        async with keepalive_lock:
            cfg = dict(keepalive_config)
        if cfg["enabled"]:
            async with keepalive_lock:
                keepalive_config["last_run"] = datetime.now(timezone.utc).isoformat()
                keepalive_config["last_result"] = "执行中..."
            if cfg.get("mode") == "sms":
                result = await _keepalive_execute_sms(
                    cfg.get("sms_number", "10086"),
                    cfg.get("sms_text", "666"),
                )
            else:
                result = await _keepalive_execute(cfg["url"])
            async with keepalive_lock:
                keepalive_config["last_run"] = datetime.now(timezone.utc).isoformat()
                keepalive_config["last_result"] = result
        interval = max(cfg["interval_hours"], 1) * 3600
        await asyncio.sleep(interval)


@app.on_event("startup")
async def startup():
    try:
        await modem.connect()
    except Exception as e:
        logger.warning("Modem not available at %s: %s", PORT, e)
    asyncio.create_task(_poll_modem_state())
    asyncio.create_task(_refresh_sms_cache())
    asyncio.create_task(_keepalive_loop())


@app.on_event("shutdown")
async def shutdown():
    await modem.disconnect()


@app.get("/api/status")
async def get_status():
    global modem_state
    return modem_state


@app.get("/api/sms")
async def list_sms():
    async with sms_cache_lock:
        if sms_cache:
            return {"messages": list(sms_cache)}
    try:
        msgs = await _list_sms()
        async with sms_cache_lock:
            sms_cache.clear()
            sms_cache.extend(msgs)
        return {"messages": msgs}
    except Exception as e:
        raise HTTPException(500, str(e))


class SendSMSRequest(BaseModel):
    number: str
    text: str


@app.post("/api/sms/send")
async def send_sms(req: SendSMSRequest):
    if not modem.connected:
        raise HTTPException(503, "Modem not connected")
    try:
        await modem.send_command("AT+CMGF=1", timeout=2)
        ref = await modem.send_sms(req.number, req.text, timeout=60)
        return {"success": True, "ref": ref}
    except ATError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/sms/{index}")
async def delete_sms(index: int):
    if not modem.connected:
        raise HTTPException(503, "Modem not connected")
    try:
        await modem.send_command(f"AT+CMGD={index}", timeout=3)
        return {"success": True}
    except ATError as e:
        raise HTTPException(400, str(e))


class KeepaliveConfig(BaseModel):
    enabled: bool
    mode: str = "url"
    url: str = ""
    sms_number: str = ""
    sms_text: str = ""
    interval_hours: int = 24


@app.get("/api/keepalive")
async def get_keepalive():
    async with keepalive_lock:
        return dict(keepalive_config)


@app.put("/api/keepalive")
async def update_keepalive(cfg: KeepaliveConfig):
    async with keepalive_lock:
        keepalive_config["enabled"] = cfg.enabled
        keepalive_config["mode"] = cfg.mode
        if cfg.url:
            keepalive_config["url"] = cfg.url
        if cfg.sms_number:
            keepalive_config["sms_number"] = cfg.sms_number
        if cfg.sms_text:
            keepalive_config["sms_text"] = cfg.sms_text
        if cfg.interval_hours >= 1:
            keepalive_config["interval_hours"] = cfg.interval_hours
    return {"success": True}


@app.post("/api/keepalive/run")
async def run_keepalive_now():
    async with keepalive_lock:
        cfg = dict(keepalive_config)
    if cfg.get("mode") == "sms":
        asyncio.create_task(
            _keepalive_execute_sms(
                cfg.get("sms_number", "10086"),
                cfg.get("sms_text", "666"),
            )
        )
    else:
        asyncio.create_task(_keepalive_execute(cfg["url"]))
    return {"success": True, "message": "保号任务已触发"}


class ATCommandRequest(BaseModel):
    command: str
    timeout: int = 10


class BalanceQuery(BaseModel):
    ussd_code: str = "*100#"


balance_cache: dict = {"result": None, "updated_at": None}
balance_cache_lock = asyncio.Lock()


@app.get("/api/balance")
async def get_balance():
    async with balance_cache_lock:
        return dict(balance_cache)


@app.post("/api/balance/query")
async def query_balance(req: BalanceQuery = BalanceQuery()):
    if not modem.connected:
        raise HTTPException(503, "Modem not connected")
    try:
        await modem.send_command("AT+CUSD=1", timeout=2)
        r = await modem.send_command(f'AT+CUSD=1,"{req.ussd_code}",15', timeout=30)
        text = ""
        for line in r:
            if "+CUSD:" in line:
                parts = line.split('"')
                if len(parts) > 1:
                    raw = parts[1]
                    decoded = _decode_ucs2(raw)
                    text = decoded if decoded != raw else raw
                    break
        result = text or "\n".join(r)
        async with balance_cache_lock:
            balance_cache["result"] = result
            balance_cache["updated_at"] = datetime.now(timezone.utc).isoformat()
        return {"success": True, "result": result}
    except ATError as e:
        error_msg = str(e)
        if "+CME ERROR: 4" in error_msg:
            detail = (
                "USSD 查询被模块拒绝 (CME ERROR: 4)，可能原因：\n"
                "1. SIM 卡未正确注册到网络 — 检查 AT+CREG? 返回状态\n"
                "2. 运营商不支持 USSD 交互 — 部分 IoT 资费卡禁用此功能\n"
                "3. 模块不支持 USSD — 确认模块固件支持 CUSD 命令\n"
                "4. SIM 卡欠费或停机\n"
                "建议：先通过 /api/status 确认网络注册状态和信号强度"
            )
        else:
            detail = f"USSD 查询失败: {error_msg}"
        raise HTTPException(400, detail)


@app.post("/api/at/command")
async def send_at_command(req: ATCommandRequest):
    if not modem.connected:
        raise HTTPException(503, "Modem not connected")
    try:
        resp = await modem.send_command(req.command, timeout=req.timeout)
        return {"success": True, "response": resp}
    except ATError as e:
        return {"success": False, "response": e.response, "error": str(e)}
    except Exception as e:
        raise HTTPException(500, str(e))


app.mount("/", StaticFiles(directory="static", html=True), name="static")


WEB_HOST = os.getenv("SMS_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("SMS_WEB_PORT", "8000"))


def main():

    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)


if __name__ == "__main__":
    main()

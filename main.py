import asyncio
import logging
import os
import re
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from at_serial import ATSerial, ATError

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
    "url": "http://www.baidu.com",
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
    if not s or len(s) < 16 or len(s) % 4 != 0:
        return text
    if not re.fullmatch(r'[0-9A-Fa-f]+', s):
        return text
    try:
        raw = bytes.fromhex(s)
        return raw.decode("utf-16-be")
    except (ValueError, UnicodeDecodeError):
        return text


async def _list_sms() -> list[dict]:
    if not modem.connected:
        return []
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
        m = re.match(r'\+CMGL:\s*(\d+),"(.*?)","(.*?)"(?:,[^,]*)?,"(.*?)"', line)
        if m:
            idx = int(m.group(1))
            status = m.group(2)
            number = m.group(3)
            date_raw = m.group(4)

            text_parts: list[str] = []
            i += 1
            while i < len(r) and not r[i].startswith("+CMGL:") and r[i] != "":
                text_parts.append(r[i])
                i += 1
            raw_text = "\n".join(text_parts).strip()
            text = _decode_ucs2(raw_text)

            parsed_date = date_raw.replace("+", " ").strip()
            messages.append({
                "index": idx,
                "status": status,
                "number": number,
                "date": parsed_date,
                "text": text,
            })
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


async def _keepalive_loop():
    while True:
        async with keepalive_lock:
            cfg = dict(keepalive_config)
        if cfg["enabled"] and cfg["url"]:
            async with keepalive_lock:
                keepalive_config["last_run"] = datetime.now(timezone.utc).isoformat()
                keepalive_config["last_result"] = "执行中..."
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
    url: str
    interval_hours: int = 24


@app.get("/api/keepalive")
async def get_keepalive():
    async with keepalive_lock:
        return dict(keepalive_config)


@app.put("/api/keepalive")
async def update_keepalive(cfg: KeepaliveConfig):
    async with keepalive_lock:
        keepalive_config["enabled"] = cfg.enabled
        if cfg.url:
            keepalive_config["url"] = cfg.url
        if cfg.interval_hours >= 1:
            keepalive_config["interval_hours"] = cfg.interval_hours
    return {"success": True}


@app.post("/api/keepalive/run")
async def run_keepalive_now():
    async with keepalive_lock:
        url = keepalive_config["url"]
    asyncio.create_task(_keepalive_execute(url))
    return {"success": True, "message": "保号任务已触发"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")

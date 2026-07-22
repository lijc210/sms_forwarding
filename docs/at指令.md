# ML307A 常用 AT 指令速查手册

> 测试环境：Ubuntu + ML307A USB 直连
> AT 口：`/dev/ttyUSB2`（波特率 115200）
>
> 通用测试格式：
> ```bash
> echo -e "指令\r\n" > /dev/ttyUSB2
> ```
> 建议开两个终端，一个 `cat /dev/ttyUSB2` 持续监听，一个负责发送指令。

---

## 一、基础测试与信息查询

| 指令 | 说明 |
|------|------|
| `AT` | 测试通信是否正常，正常返回 `OK` |
| `ATI` | 查询模块信息（厂商、型号、固件版本） |
| `AT+CGMI` | 查询模块厂商 |
| `AT+CGMM` | 查询模块型号 |
| `AT+CGMR` | 查询固件版本 |
| `AT+CGSN` | 查询模块 IMEI 号 |
| `AT+CIMI` | 查询 SIM 卡 IMSI 号 |
| `AT+CCID` | 查询 SIM 卡 ICCID 号 |
| `ATE0` / `ATE1` | 关闭/开启指令回显 |
| `AT&F` | 恢复出厂设置 |
| `AT&W` | 保存当前设置 |

---

## 二、SIM 卡与网络状态

| 指令 | 说明 |
|------|------|
| `AT+CPIN?` | 查询 SIM 卡状态（`READY` 表示正常，无需 PIN 码） |
| `AT+CPIN=<pin>` | 输入 SIM 卡 PIN 码解锁 |
| `AT+CNUM` | 查询本机号码（部分卡不存号码，可能只返回 `OK`） |
| `AT+CSQ` | 查询信号强度，返回 `+CSQ: <rssi>,<ber>`，rssi 0-31 越大越好 |
| `AT+COPS?` | 查询当前注册的运营商 |
| `AT+COPS=?` | 搜索附近可用运营商列表（耗时较长） |
| `AT+COPS=0` | 设置为自动选网模式 |
| `AT+CREG?` | 查询网络注册状态（2G/3G） |
| `AT+CEREG?` | 查询 LTE 网络注册状态 |
| `AT+CGATT?` | 查询 GPRS/分组域附着状态 |
| `AT+CPSI?` | 查询详细的网络驻留信息（频段、小区ID等，部分模块支持） |

---

## 三、短信相关

| 指令 | 说明 |
|------|------|
| `AT+CMGF=1` | 设置短信为文本模式（推荐，便于阅读） |
| `AT+CMGF=0` | 设置短信为 PDU 模式 |
| `AT+CMGF?` | 查询当前短信模式 |
| `AT+CSCS=?` | 查询模块支持的字符集 |
| `AT+CSCS="GSM"` | 设置字符集为 GSM 默认（若模块支持） |
| `AT+CPMS?` | 查询短信存储状态（已用/总容量） |
| `AT+CPMS=?` | 查询支持的存储介质（SM=SIM卡, ME=模块内存） |
| `AT+CPMS="ME","ME","ME"` | 设置短信存储在模块内存（通常容量更大） |
| `AT+CNMI=2,1,0,0,0` | 开启新短信到达主动上报（`+CMTI` 提示） |
| `AT+CMGL="ALL"` | 列出全部短信 |
| `AT+CMGL="REC UNREAD"` | 只列出未读短信 |
| `AT+CMGR=<index>` | 读取指定序号的短信 |
| `AT+CMGD=<index>` | 删除指定序号短信 |
| `AT+CMGD=1,4` | 删除全部短信 |
| `AT+CMGS="<号码>"` | 发送短信（发送后输入内容，以 `Ctrl+Z` (0x1A) 结束） |

**收到新短信的典型流程：**
```
+CMTI: "ME",1          ← 主动上报，1为索引号
AT+CMGR=1              ← 读取该条短信
```

**UCS2 编码短信解码（Python）：**
```python
def decode_ucs2(hex_str):
    return bytes.fromhex(hex_str.strip()).decode('utf-16-be')
```

---

## 四、USSD 查询（如话费余额）

| 指令 | 说明 |
|------|------|
| `AT+CUSD=1` | 开启 USSD 结果自动上报 |
| `AT+CUSD=1,"*100#",15` | 发送 USSD 码（如查余额），15 表示 GSM 文本编码 |
| `AT+CUSD=1,"<回复内容>",15` | 多级菜单场景下，回复运营商的菜单选项 |

返回示例：
```
+CUSD: 0,"您的话费余额为XX.XX元",15
```
- 首位 `0` 表示会话结束；`1` 表示还有后续菜单需要继续交互
- 若返回内容为十六进制乱码，多为 UCS2 编码，需手动解码

---

## 五、PDP 上下文 / 联网相关

| 指令 | 说明 |
|------|------|
| `AT+CGDCONT?` | 查询当前 PDP 上下文配置 |
| `AT+CGDCONT=1,"IP","<APN>"` | 设置 PDP 上下文（APN） |
| `AT+CGACT=1,1` | 激活 PDP 上下文 |
| `AT+CGACT=0,1` | 去激活 PDP 上下文 |
| `AT+CGPADDR=1` | 查询已分配的 IP 地址 |
| `AT+CGDATA="PPP",1` | 通过 PPP 方式拨号联网 |

---

## 六、GPS 定位相关（如模块支持定位功能）

| 指令 | 说明 |
|------|------|
| `AT+CGPS=1` | 开启 GPS |
| `AT+CGPS=0` | 关闭 GPS |
| `AT+CGPSINFO` | 查询当前定位信息（经纬度、时间等） |

---

## 七、电话相关（如需语音功能）

| 指令 | 说明 |
|------|------|
| `ATD<号码>;` | 拨打电话（末尾分号不能少） |
| `ATA` | 接听来电 |
| `ATH` | 挂断电话 |
| `AT+CLCC` | 查询当前通话状态 |

---

## 附：常见 CME/CMS 错误码速查

| 错误码 | 含义 |
|--------|------|
| `+CME ERROR: 50` | 参数不正确（Incorrect parameters） |
| `+CME ERROR: 10` | SIM 卡未插入 |
| `+CME ERROR: 11` | 需要 SIM 卡 PIN 码 |
| `+CME ERROR: 30` | 无网络服务 |
| `+CMS ERROR: 500` | 未知错误（短信相关） |
| `+CMS ERROR: 321` | 无效的短信存储索引 |

---

## 附：常用命令行操作模板

**串口初始化（避免立即退出/波特率异常问题）：**
```bash
stty -F /dev/ttyUSB2 115200 raw -echo min 1 time 0
```

**监听 + 发送（推荐两个终端配合）：**
```bash
# 终端A：持续监听
cat /dev/ttyUSB2

# 终端B：发送指令
echo -e "AT+CSQ\r\n" > /dev/ttyUSB2
```

**排查端口占用：**
```bash
sudo lsof /dev/ttyUSB2
sudo fuser -v /dev/ttyUSB2
sudo systemctl stop ModemManager   # 避免被抢占
```
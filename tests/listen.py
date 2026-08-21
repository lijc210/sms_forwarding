import serial

ser = serial.Serial('/dev/ttyUSB2', 115200, timeout=1)
print("正在监听...(Ctrl+C 退出)")
try:
    while True:
        data = ser.read(ser.in_waiting or 1)
        if data:
            print(data.decode(errors='ignore'), end='', flush=True)
except KeyboardInterrupt:
    pass
finally:
    ser.close()

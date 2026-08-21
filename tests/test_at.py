import serial
import time

def send_at(command, port='/dev/ttyUSB2', baudrate=115200, wait=0.5):
    ser = serial.Serial(port, baudrate, timeout=1)
    ser.write((command + '\r\n').encode())
    time.sleep(wait)
    response = ser.read(ser.in_waiting or 1024).decode(errors='ignore')
    ser.close()
    return response

# 使用示例
print(send_at('AT'))
# print(send_at('ATI'))
# print(send_at('AT+CSQ'))
# print(send_at('AT+CPIN?'))
# print(send_at('AT+CUSD=?'))

# print(send_at('AT+CREG?'))
# print(send_at('AT+CEREG?'))
# print(send_at('AT+COPS?'))
# print(send_at('AT+CUSD=1'))



# print(send_at('AT+CSCA?'))
# print(send_at('AT+CSCA?'))

print(send_at('AT+CNMI?'))
print(send_at('AT+CPMS?'))
print(send_at('AT+CPMS="ME","ME","ME"'))



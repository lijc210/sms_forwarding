注册 VID:PID
echo "2ecc 3012" | sudo tee /sys/bus/usb-serial/drivers/option1/new_id

ls -l /dev/ttyUSB*

绑定开机自动生效

sudo tee /etc/udev/rules.d/99-ml307a.rules << 'EOF'
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="2ecc", ATTR{idProduct}=="3012", RUN+="/bin/sh -c 'echo 2ecc 3012 > /sys/bus/usb-serial/drivers/option1/new_id'"
EOF

sudo udevadm control --reload-rules
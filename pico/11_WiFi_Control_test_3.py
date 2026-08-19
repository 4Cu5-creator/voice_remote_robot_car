from robot_car import RobotCar
import network
import socket
import machine
from machine import ADC, Pin
import ssd1306

# ---- OLED ----
sda = machine.Pin(10)
scl = machine.Pin(11)
i2c = machine.I2C(1, sda=sda, scl=scl, freq=400000)

oled = ssd1306.SSD1306_I2C(128, 64, i2c)

coeff = 3.3 / 65535
Pin(25, Pin.OUT).value(1)
Pin(29, Pin.IN)
a1 = ADC(29)

vin = a1.read_u16() * coeff * 3
print('Vsys = {}'.format(vin))
oled.rect(0, 0, 127, 63, 1)
oled.text("BAT Voltage", 5, 6, 1)
oled.text('    {:.3f} V'.format(vin), 5, 18, 1)
oled.show()

# Replace the following with your WIFI Credentials
SSID = "spacecamp"
SSID_PASSWORD = "space2022"

print("Connecting to your wifi...")

sta_if = network.WLAN(network.STA_IF)
if not sta_if.isconnected():
    print('connecting to network...')
    sta_if.active(True)
    sta_if.connect(SSID, SSID_PASSWORD)
    while not sta_if.isconnected():
        pass
print('Connected! Network config:', sta_if.ifconfig())

oled.text("IP Address", 5, 35, 1)
oled.text(sta_if.ifconfig()[0], 15, 47, 1)
oled.show()

# Pico W GPIO Pin
FRONT_LEFT_MOTOR_PIN_1 = 0
FRONT_LEFT_MOTOR_PIN_2 = 1
FRONT_RIGHT_MOTOR_PIN_1 = 2
FRONT_RIGHT_MOTOR_PIN_2 = 3
REAR_LEFT_MOTOR_PIN_1 = 4
REAR_LEFT_MOTOR_PIN_2 = 5
REAR_RIGHT_MOTOR_PIN_1 = 6
REAR_RIGHT_MOTOR_PIN_2 = 7

motor_pins = [FRONT_LEFT_MOTOR_PIN_1, FRONT_LEFT_MOTOR_PIN_2, FRONT_RIGHT_MOTOR_PIN_1, FRONT_RIGHT_MOTOR_PIN_2,
              REAR_LEFT_MOTOR_PIN_1, REAR_LEFT_MOTOR_PIN_2, REAR_RIGHT_MOTOR_PIN_1, REAR_RIGHT_MOTOR_PIN_2]

# Create an instance of our robot car
robot_car = RobotCar(motor_pins, 50)

robot_car.blink(5)

car_commands = {
    "forward": robot_car.move_forward,
    "reverse": robot_car.move_backward,
    "left": robot_car.turn_left,
    "right": robot_car.turn_right,
    "stop": robot_car.stop,
}

# ---- Raw TCP command server (no HTTP / Microdot / WebSocket handshake) ----
# Line-delimited text protocol: client sends one command per line
# ("forward", "reverse", "left", "right", "stop", or "speed:20"),
# server replies with one line ("OK" or "ERR:<reason>").
PORT = 8765
CLIENT_TIMEOUT_SEC = 30


def handle_line(line):
    line = line.strip()
    if not line:
        return None
    if line.startswith("speed"):
        parts = line.split(":")
        if len(parts) > 1:
            try:
                robot_car.change_speed(parts[1].strip())
                return "OK"
            except Exception as e:
                return "ERR:" + str(e)
        return "ERR:bad speed message"
    command = car_commands.get(line)
    if command is not None:
        command()
        return "OK"
    return "ERR:unknown command"


def run_server():
    addr = socket.getaddrinfo("0.0.0.0", PORT)[0][-1]
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(addr)
    server.listen(1)
    print("Command server listening on port", PORT)

    oled.fill_rect(0, 52, 127, 11, 0)
    oled.text("Port {}".format(PORT), 5, 53, 1)
    oled.show()

    while True:
        client, client_addr = server.accept()
        client.settimeout(CLIENT_TIMEOUT_SEC)
        print("Client connected:", client_addr)
        buf = b""
        try:
            while True:
                chunk = client.recv(128)
                if not chunk:
                    break  # client closed the connection
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    reply = handle_line(line.decode())
                    if reply is not None:
                        client.send((reply + "\n").encode())
        except OSError as e:
            print("Client error:", e)
        finally:
            client.close()
            print("Client disconnected")


if __name__ == "__main__":
    try:
        run_server()
    except KeyboardInterrupt:
        robot_car.deinit()
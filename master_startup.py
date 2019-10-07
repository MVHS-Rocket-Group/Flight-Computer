import time
import csv
import os
import smbus
import enum
import math
import datetime
import subprocess
import picamera
import BMP280 as barometer
import RPi.GPIO as io
from imu_utils import IMU

esc_pwm_pin = 15
# frequency (Hz) = 1 / period (sec)
esc_pwm_freq = 1 / 0.02
# Duty cycle percentage when firewalling the throttle.
esc_max_duty = 100 * 2.0 / 20.0
# Duty cycle percentage when at idle throttle.
esc_min_duty = 100 * 1.0 / 20.0
# Handle for control over the ESC PWM pin. Setup later in `try` block.
esc_pwm = None

# Conversion from encoded value to m/s^2
IMU_ACC_COEFF = 0.244 / 1000.0 * 9.81
# Conversion from encoded value to deg/s
IMU_GYRO_COEFF = 0.070  # Gyro deg/s/ per LSB.

# Loop period (sec)
loop_period = 1.0 / 10.0


class FlightState(enum):
    # Python Enum type: https://www.geeksforgeeks.org/enum-in-python
    ON_PAD = 1
    LAUNCHED = 2
    IN_FREEFALL = 3
    LANDED = 4


class IMUData:
    # Units:
    # - time: seconds
    # - acc: [x, y, z] m/s^2
    # - gyro: [x, y, z] deg/s
    # - mag: [x, y, z] µTesla?
    # - baro: [degC, pressure] deg celsius, hPa

    # https://stackoverflow.com/questions/68645/are-static-class-variables-possible-in-python
    start_time = None

    def __init__(self, flight_state, time=None, acc=None, gyro=None, mag=None, baro=None):
        # TODO: Add Barometer recording
        self.flight_state = flight_state

        if(time == None):
            if(start_time == None):
                start_time == datetime.datetime.now()
            self.time = datetime.datetime.now() - start_time
        else:
            self.time = time

        if(acc == None):
            # AHEM... correct axes.
            # self.acc = [IMU.readACCy() * IMU_ACC_COEFF, IMU.readACCx()
            #             * IMU_ACC_COEFF, -IMU.readACCz() * IMU_ACC_COEFF]
            self.acc = [IMU.readACCx() * 0.244 / 1000, IMU.readACCy() * 0.244 /
                        1000, IMU.readACCz() * 0.244 / 1000]
        else:
            self.acc = acc

        if(gyro == None):
            # AHEM... correct axes.
            # self.gyro = [-IMU.readGYRy() * IMU_GYRO_COEFF, IMU.readGYRx()
            #              * IMU_GYRO_COEFF,  IMU.readGYRz() * IMU_GYRO_COEFF]
            self.gyro = [IMU.readGYRx() * GYRO_GAIN, IMU.readGYRy() *
                         GYRO_GAIN, IMU.readGYRz() * GYRO_GAIN]
        else:
            self.gyro = gyro

        if(mag == None):
            # DODO: Conversion needed?
            self.mag = [IMU.readMAGx(), IMU.readMAGy(), IMU.readMAGz()]
        else:
            self.mag = mag

        if(baro == None):
            tuple = barometer.getBaroValues()
            self.baro = [tuple[0], tuple[1]]
        else:
            self.baro = baro

    def add_event(self, event):
        self.events.append(event)

    def get_acc_magnitude(self):
        return math.sqrt(math.pow(acc[0], 2) + math.pow(acc[1], 2) + math.pow(acc[2], 2))

    def get_gyro_magnitude(self):
        return math.sqrt(math.pow(gyro[0], 2) + math.pow(gyro[1], 2) + math.pow(gyro[2], 2))

    def get_mag_magnitude(self):
        return math.sqrt(math.pow(mag[0], 2) + math.pow(mag[1], 2) + math.pow(mag[2], 2))

    def formatted_for_log(self):
        events_as_string = ""
        for event in self.events:
            events_as_string += event + ", "

        return [time, flight_state.name, acc[0], acc[1], acc[2], gyro[0], gyro[1], gyro[2],
                mag[0], mag[1], mag[2], baro[0], baro[1], events_as_string]


# Stores which state of flight the rocket is currently in.
current_flight_state = FlightState.ON_PAD

# Placeholder for datetime object.
launch_time = None

cam = picamera.PiCamera()
# camera.resolution = (640, 480)
cam.resolution = (1280, 720)


try:
    # Init GPIO PWM output for ESC
    io.setwarnings(False)
    io.setmode(io.BOARD)
    io.setup(esc_pwm_pin, io.OUT)

    # Setup the PWM pin and set it to min command.
    esc_pwm = io.PWM(esc_pwm_pin, esc_pwm_freq)
    esc_pwm.start(esc_min_duty)

    # TODO: Spin up both fans for a second or two.

    # Init IMU
    IMU.detectIMU()     # Detect if BerryIMUv1 or BerryIMUv2 is connected.
    IMU.initIMU()       # Initialise the accelerometer, gyroscope and compass

    # Start log file
    log_folder = "flight_logs/"
    log_file = "flight_log"
    log_file_suffix = 0
    if not os.path.exists(log_folder):
        os.makedirs(log_folder)
    while os.path.isfile(log_folder + log_file + str(log_file_suffix) + ".csv"):
        log_file_suffix += 1

    log_file = csv.writer(open(log_folder + log_file + str(log_file_suffix) + ".csv", 'w'),
                          delimiter=',')
    log_file.writerow(["elapsed time", "flight state", "accX", "accY", "accZ", "gyroX",
                       "gyroY", "gyroZ", "magX", "magY", "magZ", "baroTemp", "baroPressure", "events"])

    # https://picamera.readthedocs.io/en/release-1.13/api_camera.html#picamera.PiCamera.start_recording
    recording_folder = "recordings/"
    recording_file = 0
    while os.path.isfile(recording_folder + str(recording_file) + ".h264"):
        recording_file += 1

    print("Started recording to file " + str(recording_file) + ".h264 ...")
    cam.start_recording(recording_folder +
                        str(recording_file) + ".mp4", format="h264")

    # Main Loop
    while True:
        # Read in motion data from IMU.
        imu_data = IMUData(current_flight_state)

        # TODO: Compute and send target to ESC
        if current_flight_state == FlightState.ON_PAD:
            # Idle until we detect a launch.
            esc_pwm.ChangeDutyCycle(esc_min_duty)

            # If we're seeing >2g's, then we've almost certainly launched.
            if(imu_data.get_acc_magnitude() > 2 * 9.81):
                current_flight_state = FlightState.LAUNCHED
                launch_time = datetime.datetime.now()
                imu_data.add_event("launched")
                esc_pwm.ChangeDutyCycle(esc_max_duty)
        elif current_flight_state == FlightState.LAUNCHED:
            if datetime.datetime.now() > launch_time + datetime.timedelta(seconds=10):
                # Begin shutdown procedure
                imu_data.add_event("FTS automatic trigger: 10s from launch")
                log_file.writerow(imu_data.formatted_for_log())
                break

            # If we're seeing <2g's, then we've almost certainly reached apogee.
            if(imu_data.get_acc_magnitude() > 2 * 9.81):
                current_flight_state = FlightState.IN_FREEFALL
                imu_data.add_event("in freefall")
        elif current_flight_state == FlightState.IN_FREEFALL:
            pass
        elif current_flight_state == FlightState.LANDED:
            pass

        # TODO: Did any important events get triggered?
        # TODO: Did the flight state change? e.g. did we just launch, just land?
        # TODO: Has it been >1 munute since landing? If so, shut off the camera recording and shut down RPi

        # Log current system state to file
        log_file.writerow(imu_data.formatted_for_log())

        time.sleep(loop_period)

except KeyboardInterrupt:
    imu_data = IMUData(current_flight_state)
    imu_data.add_event("manually terminated by SIGTERM")
    log_file.writerow(imu_data.formatted_for_log())
    pass

cam.stop_recording()
esc_pwm.ChangeDutyCycle(esc_min_duty)
time.sleep(1)
esc_pwm.stop()
gpio.cleanup()


# ENABLE FOR FLIGHT: (MUST BE RUN AS SUPER USER FOR THIS TO WORK!!!!!!!!!!)
# subprocess.call(["sudo", "shutdown", "-h", "now"])

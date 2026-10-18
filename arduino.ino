
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// =====================================================
// WattWise Smart Tariff - Arduino Firmware
// =====================================================

// LCD configuration
LiquidCrystal_I2C lcd(0x27, 16, 2);

// Sensor pins
const int VOLTAGE1_PIN = A0;
const int CURRENT1_PIN = A1;
const int VOLTAGE2_PIN = A2;
const int CURRENT2_PIN = A3;

// Relay and button pins
const int RELAY1_PIN = 7;
const int RELAY2_PIN = 8;
const int BUTTON_PIN = 2;

// Most relay modules are active LOW.
// If your relay board works the opposite way, change these.
const int RELAY_ON_LEVEL  = LOW;
const int RELAY_OFF_LEVEL = HIGH;

// Sensor calibration: adjust for your actual sensor modules.
const float ADC_REFERENCE = 5.0;
const float ADC_MAX_VALUE = 1023.0;

// Example calibration values; calibrate against known measurements.
const float VOLTAGE_CALIBRATION = 5.0;
const float ACS712_SENSITIVITY = 0.066; // 30A ACS712: 66 mV/A

// Current sensor zero-current voltage.
// This is measured at startup.
float current1Zero = 2.5;
float current2Zero = 2.5;

// Relay state: false = OFF, true = ON
bool relay1State = false;
bool relay2State = false;

// LCD display selection
int displayedLoad = 1;

// Button debounce
int lastButtonReading = HIGH;
int stableButtonState = HIGH;
unsigned long lastDebounceTime = 0;
const unsigned long DEBOUNCE_DELAY = 50;

// Timing
unsigned long lastSensorRead = 0;
unsigned long lastLcdUpdate = 0;
const unsigned long SENSOR_INTERVAL = 500;
const unsigned long LCD_INTERVAL = 500;

// Serial input buffer
String serialCommand = "";

// =====================================================
// Relay control
// =====================================================

void setRelay(int load, bool turnOn) {
  if (load == 1) {
    relay1State = turnOn;
    digitalWrite(
      RELAY1_PIN,
      turnOn ? RELAY_ON_LEVEL : RELAY_OFF_LEVEL
    );
  } else if (load == 2) {
    relay2State = turnOn;
    digitalWrite(
      RELAY2_PIN,
      turnOn ? RELAY_ON_LEVEL : RELAY_OFF_LEVEL
    );
  } else {
    return;
  }

  Serial.print("ACK,");
  Serial.print(load);
  Serial.print(",");
  Serial.println(turnOn ? "ON" : "OFF");
}

// =====================================================
// Serial command handling
// Supports:
// 1:ON
// 1:OFF
// 2:ON
// 2:OFF
// RELAY,1,ON
// RELAY,1,OFF
// RELAY,2,ON
// RELAY,2,OFF
// =====================================================

void processCommand(String command) {
  command.trim();
  command.toUpperCase();

  if (command.length() == 0) {
    return;
  }

  // Format: 1:ON or 2:OFF
  if (command.startsWith("1:")) {
    if (command.substring(2) == "ON") {
      setRelay(1, true);
    } else if (command.substring(2) == "OFF") {
      setRelay(1, false);
    }
    return;
  }

  if (command.startsWith("2:")) {
    if (command.substring(2) == "ON") {
      setRelay(2, true);
    } else if (command.substring(2) == "OFF") {
      setRelay(2, false);
    }
    return;
  }

  // Format: RELAY,load,state
  if (command.startsWith("RELAY,")) {
    int firstComma = command.indexOf(',');
    int secondComma = command.indexOf(',', firstComma + 1);

    if (secondComma > 0) {
      int load = command.substring(
        firstComma + 1, secondComma
      ).toInt();

      String state = command.substring(secondComma + 1);
      state.trim();

      if (state == "ON") {
        setRelay(load, true);
      } else if (state == "OFF") {
        setRelay(load, false);
      }
    }
  }
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    char incoming = Serial.read();

    if (incoming == '\n' || incoming == '\r') {
      if (serialCommand.length() > 0) {
        processCommand(serialCommand);
        serialCommand = "";
      }
    } else {
      if (serialCommand.length() < 80) {
        serialCommand += incoming;
      } else {
        serialCommand = "";
      }
    }
  }
}

// =====================================================
// Sensor readings
// =====================================================

// Average analog samples to reduce noise.
float readAverageVoltage(int pin) {
  const int samples = 100;
  long total = 0;

  for (int i = 0; i < samples; i++) {
    total += analogRead(pin);
  }

  float averageADC = (float)total / samples;
  return averageADC * ADC_REFERENCE / ADC_MAX_VALUE;
}

// Approximate AC voltage measurement.
// This requires calibration for your voltage sensor and supply.
float readVoltageRMS(int pin) {
  const int samples = 500;
  float mean = 0.0;
  float sumSquares = 0.0;

  for (int i = 0; i < samples; i++) {
    float voltage = analogRead(pin) *
                    ADC_REFERENCE / ADC_MAX_VALUE;

    mean += voltage;
    sumSquares += voltage * voltage;
  }

  mean /= samples;
  float meanSquare = sumSquares / samples;
  float variance = meanSquare - (mean * mean);

  if (variance < 0) {
    variance = 0;
  }

  float sensorRMS = sqrt(variance);
  return sensorRMS * VOLTAGE_CALIBRATION;
}

// Approximate AC current RMS measurement using ACS712.
float readCurrentRMS(int pin, float zeroVoltage) {
  const int samples = 500;
  float sumSquares = 0.0;

  for (int i = 0; i < samples; i++) {
    float sensorVoltage = analogRead(pin) *
                          ADC_REFERENCE / ADC_MAX_VALUE;

    float current = (sensorVoltage - zeroVoltage) /
                    ACS712_SENSITIVITY;

    sumSquares += current * current;
  }

  float currentRMS = sqrt(sumSquares / samples);

  // Treat small sensor noise as zero.
  if (currentRMS < 0.08) {
    currentRMS = 0.0;
  }

  return currentRMS;
}

// Measure the ACS712 zero-current output at startup.
// Keep both loads OFF during this calibration.
void calibrateCurrentSensors() {
  const int samples = 500;
  float total1 = 0.0;
  float total2 = 0.0;

  for (int i = 0; i < samples; i++) {
    total1 += analogRead(CURRENT1_PIN) *
              ADC_REFERENCE / ADC_MAX_VALUE;

    total2 += analogRead(CURRENT2_PIN) *
              ADC_REFERENCE / ADC_MAX_VALUE;

    delay(2);
  }

  current1Zero = total1 / samples;
  current2Zero = total2 / samples;
}

// =====================================================
// LCD display
// =====================================================

void updateLCD(float voltage1, float current1,
               float voltage2, float current2) {
  lcd.setCursor(0, 0);

  if (displayedLoad == 1) {
    lcd.print("Load 1          ");
    lcd.setCursor(0, 1);

    lcd.print(relay1State ? "ON " : "OFF");
    lcd.print(" V:");
    lcd.print(voltage1, 0);
    lcd.print(" I:");
    lcd.print(current1, 1);
  } else {
    lcd.print("Load 2          ");
    lcd.setCursor(0, 1);

    lcd.print(relay2State ? "ON " : "OFF");
    lcd.print(" V:");
    lcd.print(voltage2, 0);
    lcd.print(" I:");
    lcd.print(current2, 1);
  }
}

// =====================================================
// Physical button: switch LCD between loads
// =====================================================

void handleButton() {
  int reading = digitalRead(BUTTON_PIN);

  if (reading != lastButtonReading) {
    lastDebounceTime = millis();
  }

  if (millis() - lastDebounceTime > DEBOUNCE_DELAY) {
    if (reading != stableButtonState) {
      stableButtonState = reading;

      if (stableButtonState == LOW) {
        displayedLoad = (displayedLoad == 1) ? 2 : 1;
      }
    }
  }

  lastButtonReading = reading;
}

// =====================================================
// Send data to laptop Flask dashboard
// CSV format:
// DATA,V1,I1,V2,I2,R1,R2,DISPLAYED_LOAD
// Relay state: 1 = ON, 0 = OFF
// =====================================================

void sendSensorData(float voltage1, float current1,
                    float voltage2, float current2) {
  Serial.print("DATA,");
  Serial.print(voltage1, 2);
  Serial.print(",");
  Serial.print(current1, 2);
  Serial.print(",");
  Serial.print(voltage2, 2);
  Serial.print(",");
  Serial.print(current2, 2);
  Serial.print(",");
  Serial.print(relay1State ? 1 : 0);
  Serial.print(",");
  Serial.print(relay2State ? 1 : 0);
  Serial.print(",");
  Serial.println(displayedLoad);
}

// =====================================================
// Setup
// =====================================================

void setup() {
  Serial.begin(9600);

  pinMode(RELAY1_PIN, OUTPUT);
  pinMode(RELAY2_PIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);

  // Set relays OFF immediately at startup.
  digitalWrite(RELAY1_PIN, RELAY_OFF_LEVEL);
  digitalWrite(RELAY2_PIN, RELAY_OFF_LEVEL);

  relay1State = false;
  relay2State = false;

  lcd.init();
  lcd.backlight();
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("WattWise Ready");
  lcd.setCursor(0, 1);
  lcd.print("Loads OFF");

  // Ensure both loads are OFF while zero-current
  // sensor offsets are measured.
  delay(1000);
  calibrateCurrentSensors();

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Sensors Ready");
  delay(1000);
  lcd.clear();

  Serial.println("WATTWISE_READY");
}

// =====================================================
// Main loop
// =====================================================

void loop() {
  readSerialCommands();
  handleButton();

  unsigned long now = millis();

  if (now - lastSensorRead >= SENSOR_INTERVAL) {
    lastSensorRead = now;

    float voltage1 = readVoltageRMS(VOLTAGE1_PIN);
    float current1 = readCurrentRMS(
      CURRENT1_PIN, current1Zero
    );

    float voltage2 = readVoltageRMS(VOLTAGE2_PIN);
    float current2 = readCurrentRMS(
      CURRENT2_PIN, current2Zero
    );

    sendSensorData(
      voltage1, current1,
      voltage2, current2
    );

    if (now - lastLcdUpdate >= LCD_INTERVAL) {
      lastLcdUpdate = now;
      updateLCD(
        voltage1, current1,
        voltage2, current2
      );
    }
  }
}

/*
  DIGISPARK GUARD - USB HID + GY-273
  Placa: Digispark ATtiny85 (16.5 MHz)

  USB:
    Usa DigiUSB (HID Feature Report), NAO DigiCDC.
    VID:PID esperado: 16C0:05DF
    Cada relatorio HID possui 1 byte de dados (sem report ID no firmware).

  GY-273:
    VCC -> alimentacao apropriada ao modulo
    GND -> GND
    SDA -> P0
    SCL -> P2

  LED onboard:
    3 piscadas no inicio: firmware iniciou
    piscando devagar: calibrando
    pulso curto a cada 2 s: armado/estavel
    aceso: movimento detectado (estado travado)
    piscando rapido: sensor ausente/falha antes de armar

  Protocolo enviado por DigiUSB (um caractere por Feature Report):
    B = boot
    C = calibrando
    A = armado e estavel
    M = movimento (travado ate reiniciar a placa)
    E = sensor ausente/falha antes de armar

  IMPORTANTE:
    O host consulta os bytes usando HIDAPI GetFeatureReport.
    Nao existe porta COM e nao use o Serial Monitor para este firmware.
*/

#include <DigiUSB.h>
#include <TinyWireM.h>

#define LED_PIN 1
#define LIMITE 80
#define TEMPO_CAL_MS 4000UL
#define AMOSTRAS_CAL 12
#define HEARTBEAT_MS 250UL
#define LOOP_MS 15

// Enderecos dos sensores mais comuns encontrados em modulos GY-273.
#define HMC_ADDR 0x1E
#define QMC_ADDR 0x0D

char estado = 'B';
char ultimoEnviado = 0;
unsigned long ultimoEnvio = 0;
unsigned long inicioCalibracao = 0;
unsigned long proximaTentativaSensor = 0;
unsigned long ultimoLed = 0;

bool sensorQMC = false;
bool sensorOK = false;
bool armado = false;
bool movimentoTravado = false;
bool primeiraAmostraCal = false;

byte enderecoSensor = HMC_ADDR;
byte contadorCal = 0;

int16_t referencia[3] = {0, 0, 0};
int16_t primeiraCal[3] = {0, 0, 0};
int32_t somaCal[3] = {0, 0, 0};

// -----------------------------------------------------------------------------
// I2C / magnetometro
// -----------------------------------------------------------------------------

bool escreverReg(byte endereco, byte reg, byte valor) {
  TinyWireM.beginTransmission(endereco);
  TinyWireM.send(reg);
  TinyWireM.send(valor);
  return TinyWireM.endTransmission() == 0;
}

bool iniciarHMC() {
  enderecoSensor = HMC_ADDR;
  sensorQMC = false;

  if (!escreverReg(enderecoSensor, 0x00, 0x70)) return false;
  if (!escreverReg(enderecoSensor, 0x01, 0x20)) return false;
  if (!escreverReg(enderecoSensor, 0x02, 0x00)) return false;
  return true;
}

bool iniciarQMC() {
  enderecoSensor = QMC_ADDR;
  sensorQMC = true;

  if (!escreverReg(enderecoSensor, 0x0B, 0x01)) return false;
  if (!escreverReg(enderecoSensor, 0x09, 0x1D)) return false;
  return true;
}

bool iniciarSensor() {
  if (iniciarHMC()) return true;
  if (iniciarQMC()) return true;
  return false;
}

bool lerSensor(int16_t v[3]) {
  TinyWireM.beginTransmission(enderecoSensor);
  TinyWireM.send(sensorQMC ? 0x00 : 0x03);
  if (TinyWireM.endTransmission() != 0) return false;

  TinyWireM.requestFrom(enderecoSensor, 6);
  if (TinyWireM.available() < 6) return false;

  byte b0 = TinyWireM.receive();
  byte b1 = TinyWireM.receive();
  byte b2 = TinyWireM.receive();
  byte b3 = TinyWireM.receive();
  byte b4 = TinyWireM.receive();
  byte b5 = TinyWireM.receive();

  if (sensorQMC) {
    // QMC5883L: X, Y, Z em little-endian. Escala reduzida para aproximar HMC.
    v[0] = (int16_t)(((uint16_t)b1 << 8) | b0) / 3;
    v[1] = (int16_t)(((uint16_t)b3 << 8) | b2) / 3;
    v[2] = (int16_t)(((uint16_t)b5 << 8) | b4) / 3;
  } else {
    // HMC5883L: X, Z, Y em big-endian.
    v[0] = (int16_t)(((uint16_t)b0 << 8) | b1);
    v[2] = (int16_t)(((uint16_t)b2 << 8) | b3);
    v[1] = (int16_t)(((uint16_t)b4 << 8) | b5);
    if (v[0] == -4096 || v[1] == -4096 || v[2] == -4096) return false;
  }
  return true;
}

int32_t diferenca(const int16_t a[3], const int16_t b[3]) {
  int32_t dx = (int32_t)a[0] - (int32_t)b[0];
  int32_t dy = (int32_t)a[1] - (int32_t)b[1];
  int32_t dz = (int32_t)a[2] - (int32_t)b[2];
  if (dx < 0) dx = -dx;
  if (dy < 0) dy = -dy;
  if (dz < 0) dz = -dz;
  return dx + dy + dz;
}

void iniciarCalibracao(unsigned long agora) {
  sensorOK = true;
  armado = false;
  contadorCal = 0;
  primeiraAmostraCal = false;
  somaCal[0] = somaCal[1] = somaCal[2] = 0;
  inicioCalibracao = agora;
  estado = 'C';
}

// -----------------------------------------------------------------------------
// USB HID (DigiUSB)
// -----------------------------------------------------------------------------

void enviarEstado() {
  unsigned long agora = millis();
  if (estado != ultimoEnviado || (agora - ultimoEnvio) >= HEARTBEAT_MS) {
    // DigiUSB usa Feature Reports HID; o Python le esse byte com GetFeatureReport.
    if (DigiUSB.write((byte)estado)) {
      ultimoEnviado = estado;
      ultimoEnvio = agora;
    }
  }
}

void atualizarLED() {
  unsigned long agora = millis();
  bool ligado = false;

  if (estado == 'M') {
    ligado = true;
  } else if (estado == 'C') {
    ligado = ((agora / 250UL) & 1U) != 0;
  } else if (estado == 'E') {
    ligado = ((agora / 100UL) & 1U) != 0;
  } else if (estado == 'A') {
    ligado = (agora % 2000UL) < 70UL;
  }

  digitalWrite(LED_PIN, ligado ? HIGH : LOW);
  ultimoLed = agora;
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  TinyWireM.begin();
  DigiUSB.begin();

  // Indicacao de que o programa principal iniciou.
  for (byte i = 0; i < 3; i++) {
    digitalWrite(LED_PIN, HIGH);
    DigiUSB.delay(70);
    digitalWrite(LED_PIN, LOW);
    DigiUSB.delay(70);
  }

  DigiUSB.write((byte)'B');
  DigiUSB.refresh();

  if (iniciarSensor()) {
    iniciarCalibracao(millis());
  } else {
    sensorOK = false;
    estado = 'E';
    proximaTentativaSensor = millis() + 1000UL;
  }
}

void loop() {
  unsigned long agora = millis();
  int16_t leitura[3];

  DigiUSB.refresh();

  // Procura/reprocura o sensor sem bloquear o atendimento USB.
  if (!sensorOK && (long)(agora - proximaTentativaSensor) >= 0) {
    proximaTentativaSensor = agora + 1500UL;
    if (iniciarSensor()) {
      iniciarCalibracao(agora);
    } else {
      estado = 'E';
    }
  }

  if (sensorOK && !movimentoTravado) {
    if (lerSensor(leitura)) {
      if (!armado) {
        if ((agora - inicioCalibracao) >= TEMPO_CAL_MS) {
          // A coleta so comeca depois do tempo inicial para posicionar o notebook.
          if (!primeiraAmostraCal) {
            primeiraCal[0] = leitura[0];
            primeiraCal[1] = leitura[1];
            primeiraCal[2] = leitura[2];
            primeiraAmostraCal = true;
            somaCal[0] = somaCal[1] = somaCal[2] = 0;
            contadorCal = 0;
          }

          // Se ainda estiver variando durante a coleta, reinicia a media.
          if (diferenca(leitura, primeiraCal) > (LIMITE * 2)) {
            primeiraCal[0] = leitura[0];
            primeiraCal[1] = leitura[1];
            primeiraCal[2] = leitura[2];
            somaCal[0] = somaCal[1] = somaCal[2] = 0;
            contadorCal = 0;
          } else {
            somaCal[0] += leitura[0];
            somaCal[1] += leitura[1];
            somaCal[2] += leitura[2];
            contadorCal++;

            if (contadorCal >= AMOSTRAS_CAL) {
              referencia[0] = somaCal[0] / AMOSTRAS_CAL;
              referencia[1] = somaCal[1] / AMOSTRAS_CAL;
              referencia[2] = somaCal[2] / AMOSTRAS_CAL;
              armado = true;
              estado = 'A';
            }
          }
        } else {
          estado = 'C';
        }
      } else {
        if (diferenca(leitura, referencia) > LIMITE) {
          movimentoTravado = true;
          estado = 'M';
        } else {
          estado = 'A';
        }
      }
    } else {
      // Perder o sensor depois de armado e tratado como violacao.
      if (armado) {
        movimentoTravado = true;
        estado = 'M';
      } else {
        sensorOK = false;
        estado = 'E';
        proximaTentativaSensor = agora + 1000UL;
      }
    }
  }

  if (movimentoTravado) estado = 'M';
  else if (!sensorOK) estado = 'E';
  else if (!armado) estado = 'C';

  enviarEstado();
  atualizarLED();

  // Mantem o V-USB atendido durante a espera.
  DigiUSB.delay(LOOP_MS);
}

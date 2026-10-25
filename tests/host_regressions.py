"""Run production C functions with fault-injected peripherals (Python 3 + cc).

The MCU headers cannot run on a host. Extract the functions verbatim, replace
only their hardware dependencies, and compile the resulting test translation
units in a temporary directory. Run: python3 tests/host_regressions.py
"""
from pathlib import Path
import re
import subprocess
import tempfile

SRC = Path(__file__).resolve().parents[1] / "V11_BMS" / "src"


def weak_cell_macros():
    """The WEAK_CELL_* helpers, taken from the header so the stubs cannot drift."""
    header = (SRC / "eeprom_handler.h").read_text()
    return "\n".join(re.findall(r"^#define WEAK_CELL_\w+(?:.*\\\n)*.*", header, re.M)) + "\n"


def function(file, name):
    source = (SRC / file).read_text()
    match = re.search(r"^[\w *]+\b" + name + r"\([^;]*?\)\n\{", source, re.M)
    assert match, name
    depth = 1
    end = match.end()
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end]


HEADER = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include "config.h"
'''

CACHE = r'''
static uint16_t bq7693_cell_voltages[7], bq7693_cached_cells[7];
static bool bq7693_cached_cells_valid, bq7693_configured = true;
static int read_count, fail_read = -1;
#define VC1_HI_BYTE 12
static bool bq7693_read_register(int addr, size_t len, uint8_t *out) {
  (void)len;
  int n = read_count++;
  if (n == fail_read) return false;
  out[0] = 15; out[1] = addr;
  return true;
}
static uint16_t bq7693_vc_to_mv(uint16_t raw) { return raw; }
'''
CACHE_MAIN = r'''
int main(void) {
  assert(bq7693_get_cached_cell_voltages() == NULL);
  assert(bq7693_get_cached_pack_voltage() == -1);
  fail_read = 2;
  bq7693_get_cell_voltages();
  assert(bq7693_get_cached_cell_voltages() == NULL);
  fail_read = -1;
  bq7693_get_cell_voltages();
  uint16_t expected[7];
  memcpy(expected, bq7693_get_cached_cell_voltages(), sizeof expected);
  int before = read_count, sum = 0;
  for (int i = 0; i < 7; ++i) sum += expected[i];
  assert(bq7693_get_cached_pack_voltage() == sum);
  assert(read_count == before); // telemetry performs no I2C
  fail_read = read_count + 3;
  bq7693_get_cell_voltages();
  assert(bq7693_cell_voltages[3] == 0); // safety sees failure
  assert(memcmp(expected, bq7693_get_cached_cell_voltages(), sizeof expected) == 0);
}
'''

TEMPERATURE = r'''
#define NTC_INVALID_TEMPERATURE INT16_MIN
#define BMS_ADC_CH_TC2 1
static int16_t pack_temperature, pack_temperature_2;
static int16_t tc1[3], tc2[3];
static int at1, at2;
static int16_t bms_read_temperature(void) { return tc1[at1++]; }
static uint16_t adc_convert_channel(int ch) { (void)ch; return (uint16_t)tc2[at2++]; }
static int16_t NTC_ADC2Temperature(uint16_t raw) { return (int16_t)raw; }
static void wdt_reset_count(void) {}
'''
TEMPERATURE_MAIN = r'''
int main(void) {
  tc1[0] = INT16_MIN; tc1[1] = 250;
  tc2[0] = tc2[1] = 270;
  assert(bms_sample_temperatures());
  assert(at1 == 2 && at2 == 1 && pack_temperature == 250 && pack_temperature_2 == 270);
  at1 = at2 = 0;
  for (int i = 0; i < 3; ++i) tc1[i] = tc2[i] = INT16_MIN;
  assert(!bms_sample_temperatures());
  assert(at1 == 3);
  at1 = at2 = 0; tc1[0] = -300; tc2[0] = 800;
  assert(bms_sample_temperatures());
  assert(at1 == 1 && pack_temperature_2 == 800); // no debounce of real temperatures
  at1 = at2 = 0; tc1[0] = INT16_MIN; tc1[1] = 250;
  tc2[0] = 500; tc2[1] = 250;
  assert(bms_sample_temperatures()); // recovered ADC must not hide valid heat
  assert(at1 == 2 && at2 == 1 && pack_temperature_2 == 500);
}
'''

SOC = r'''
static int bms_error;
#define BMS_ERR_PACK_DISCHARGED 5
#define BMS_ERR_UNDERVOLTAGE 6
static struct { int32_t current_charge_level, total_pack_capacity; } eeprom_data;
'''
SOC_MAIN = r'''
int main(void) {
  for (int32_t cap = 1200000; cap <= (int32_t)PACK_CAPACITY_UPPER_BOUND_UAH; cap += 101003) {
    eeprom_data.total_pack_capacity = cap;
    for (int32_t level = 0; level <= cap + 10000; level += 997) {
      eeprom_data.current_charge_level = level;
      uint32_t expected = (uint64_t)level * 10000 / cap;
      if (expected > 10000) expected = 10000;
      if (expected < 100) expected = 100;
      assert(bms_get_soc_x100() == expected);
    }
  }
  bms_error = BMS_ERR_UNDERVOLTAGE;
  assert(bms_get_soc_x100() == 0);
}
'''

EEPROM = r'''
#define EEPROM_PAGE_SIZE 60
#define EEPROM_MAGIC 0x424D5303u
#define STATUS_OK 0
enum eeprom_write_result { EEPROM_WRITE_FAILED=-1, EEPROM_UNCHANGED, EEPROM_WRITTEN };
struct eeprom_data {
  uint32_t magic;
  int32_t total_pack_capacity, current_charge_level;
  uint32_t full_discharge_seen, idle_wakes, weak_cell, crc32;
};
static volatile struct eeprom_data eeprom_data;
static struct eeprom_data eeprom_shadow;
static bool eeprom_ok, eeprom_shadow_valid, eeprom_data_valid;
static uint8_t flash[EEPROM_PAGE_SIZE], pending[EEPROM_PAGE_SIZE];
static int writes, corrupt_commits;
static uint32_t calc_crc32(const uint8_t *data, size_t len) {
  uint32_t hash=0; while (len--) hash = hash * 33u + *data++; return hash;
}
static int eeprom_emulator_read_page(int page, uint8_t *out) {
  (void)page; memcpy(out, flash, sizeof flash); return STATUS_OK;
}
static int eeprom_emulator_write_page(int page, const uint8_t *in) {
  (void)page; writes++; memcpy(pending, in, sizeof pending); return STATUS_OK;
}
static int eeprom_emulator_commit_page_buffer(void) {
  memcpy(flash, pending, sizeof flash);
  if (corrupt_commits > 0) { flash[4] ^= 1; corrupt_commits--; }
  return STATUS_OK; // match ASF: failure may be invisible in the status
}
'''
EEPROM_MAIN = r'''
int main(void) {
  eeprom_data.magic = EEPROM_MAGIC;
  eeprom_data.total_pack_capacity = 2000000;
  eeprom_data.current_charge_level = 2200000; // upward learning must survive reboot
  eeprom_data.full_discharge_seen = 1;
  eeprom_data.idle_wakes = STORAGE_IDLE_WAKES + 10;
  eeprom_data.weak_cell = 0x12345678u; // nonsense tracker: reset, gauge kept
  eeprom_data_valid = true;
  corrupt_commits = 1;
  assert(eeprom_write() == EEPROM_WRITTEN && writes == 2);
  assert(eeprom_read() == 0);
  assert(eeprom_data.total_pack_capacity == 2000000);
  assert(eeprom_data.current_charge_level == 2200000);
  assert(eeprom_data.idle_wakes == STORAGE_IDLE_WAKES);
  assert(eeprom_data.weak_cell == WEAK_CELL_DEFAULT);
  assert(eeprom_write() == EEPROM_WRITTEN); // normalization reaches flash
  int before = writes;
  assert(eeprom_write() == EEPROM_UNCHANGED && writes == before);
  eeprom_data.weak_cell = WEAK_CELL_MAKE(4, 1, 0); // a tracker change alone is stored
  assert(eeprom_write() == EEPROM_WRITTEN && writes == before + 1);
  assert(eeprom_read() == 0 && eeprom_data.weak_cell == WEAK_CELL_MAKE(4, 1, 0));
  before = writes;
  eeprom_data.current_charge_level += 100000;
  corrupt_commits = 3;
  assert(eeprom_write() == EEPROM_WRITE_FAILED && writes == before + 3);
  assert(!eeprom_ok && !eeprom_shadow_valid);
}
'''

NOFLOW = r'''
#undef SERIAL_DEBUG
#define SERIAL_DEBUG 0
#define PIN_PA01 1
#define BMS_SLEEP 8
#define BMS_ERR_I2C_FAIL 9
static bool current_flowing, taper_complete, healthy = true, storage;
static unsigned noflow_near_full_windows, noflow_low_samples;
static unsigned now = 1, noflow_timeout = 1;
static uint16_t voltages[7];
static int bms_state, fault, fet_off, balance_off;
static void sw_timer_start(unsigned *t) { *t = now; }
static bool sw_timer_is_elapsed(unsigned *t, unsigned timeout) {
  if (!*t || now - *t >= timeout) { *t=0; return true; } return false;
}
static void bq7693_comm_clear_error(void) {}
static uint16_t *bq7693_get_cell_voltages(void) { return voltages; }
static bool bq7693_comm_healthy(void) { return healthy; }
static bool bms_in_storage_mode(void) { return storage; }
static void port_pin_set_output_level(int pin, bool on) { (void)pin; assert(!on); }
static void bms_fet_off(bool charge) {
  assert(charge); port_pin_set_output_level(ENABLE_CHARGE_PIN, false); fet_off++;
}
static void bms_balance_stop(void) { balance_off++; }
static void bms_force_fault(int code) { fault=code; }
static void leds_off(void) {}
static void serial_debug_send_message(const char *s) { (void)s; }
'''
NOFLOW_MAIN = r'''
static void reset(void) {
  now=1; noflow_timeout=1;
  bms_state=fault=fet_off=balance_off=0;
  noflow_near_full_windows=noflow_low_samples=0;
  taper_complete=current_flowing=storage=false; healthy=true;
  for (int i=0; i<7; ++i) voltages[i]=CELL_FULL_CHARGE_RELEASE_VOLTAGE;
}
int main(void) {
  reset(); now += CHARGE_NO_CURRENT_TIMEOUT_MS;
  healthy=false;
  for (int i=0; i<7; ++i) voltages[i]=0;
  noflow_step();
  assert(fault == BMS_ERR_I2C_FAIL && bms_state != BMS_SLEEP);
  assert(fet_off && balance_off);
  reset();
  for (int i=0; i<3; ++i) {
    now += CHARGE_NO_CURRENT_TIMEOUT_MS; noflow_step();
    assert(taper_complete == (i == 2));
  }
  assert(!fault && bms_state != BMS_SLEEP);
  reset(); now += CHARGE_NO_CURRENT_TIMEOUT_MS;
  for (int i=0; i<7; ++i) voltages[i]=3800;
  noflow_step(); assert(!bms_state);
  now+=50; noflow_step(); assert(!bms_state);
  now+=50; noflow_step(); assert(bms_state == BMS_SLEEP);
  reset(); now+=CHARGE_NO_CURRENT_TIMEOUT_MS; noflow_step();
  current_flowing=true; now+=50; noflow_step();
  assert(noflow_near_full_windows == 0);
  reset(); storage=true;
  for (int i=0; i<7; ++i) voltages[i]=CELL_STORAGE_RELEASE_VOLTAGE;
  for (int i=0; i<3; ++i) { now+=CHARGE_NO_CURRENT_TIMEOUT_MS; noflow_step(); }
  assert(taper_complete);
}
'''


TRIGGER = r'''
#define BMS_PRINT(...) ((void)0)
typedef uint32_t sw_timer;
static uint32_t now = 1;
static void sw_timer_start(sw_timer *t) { *t = now; }
static bool sw_timer_is_elapsed(sw_timer *t, uint32_t timeout) {
  if (*t == 0) return true;
  if (now - *t >= timeout) { *t = 0; return true; }
  return false;
}
enum { DIO_TRIGGER_PRESSED, DIO_CHARGER_CONNECTED };
static uint8_t trigger, charger;
static bool vacuum = true;
static uint8_t dio_read(int pin) { return pin == DIO_TRIGGER_PRESSED ? trigger : charger; }
static bool dsn_prot_get_vacuum_connected(void) { return vacuum; }
static bool     bms_trigger_latched;
static sw_timer bms_trigger_latch_timer;
static bool     bms_trigger_tap_pending;
static sw_timer bms_trigger_tap_timer;
static bool     bms_trigger_swallow;
static bool bms_trigger_active(void);
'''
TRIGGER_MAIN = r'''
/* The 50ms main loop, trigger held at level for ms; returns the last answer. */
static bool run(uint32_t ms, uint8_t level, bool *ever_off) {
  bool motor = false;
  trigger = level;
  for (uint32_t t = 0; t < ms; t += 50) {
    now += 50;
    motor = bms_trigger_active();
    if (ever_off && !motor) *ever_off = true;
  }
  return motor;
}
static void settle(void) { run(2000, 0, NULL); bms_trigger_latch_clear(); run(500, 0, NULL); }
static void double_tap(bool *ever_off) { run(150, 1, ever_off); run(150, 0, ever_off); run(150, 1, ever_off); run(100, 0, ever_off); }
int main(void) {
  assert(TRIGGER_TOGGLE_MODE == 3 && TRIGGER_DOUBLE_TAP_MS == 400);
  bool off;
  settle();                                                     // hold: stock behaviour
  assert(run(100, 1, NULL) && run(1000, 1, NULL));
  assert(!run(50, 0, NULL) && !bms_trigger_latched);
  settle();                                                     // one tap: runs through the window, then stops
  run(150, 1, NULL);
  assert(run(100, 0, NULL));
  assert(!run(500, 0, NULL) && !bms_trigger_latched);
  settle(); off = false;                                        // double tap latches without a stutter
  double_tap(&off);
  assert(bms_trigger_latched && !off && run(5000, 0, NULL));
  assert(!run(50, 1, NULL));                                    // one press stops at once...
  assert(!run(1500, 1, NULL));                                  // ...holding it does not restart...
  assert(!run(50, 0, NULL) && !run(1000, 0, NULL));             // ...nor does letting go
  settle();
  assert(run(100, 1, NULL));                                    // and the next press runs again
  run(50, 0, NULL); settle();                                   // tap then hold: no latch
  run(150, 1, NULL); run(150, 0, NULL); run(600, 1, NULL); run(50, 0, NULL);
  assert(!bms_trigger_latched && !run(500, 0, NULL));
  settle();                                                     // taps too far apart: no latch
  run(150, 1, NULL); run(600, 0, NULL); run(150, 1, NULL); run(100, 0, NULL);
  assert(!bms_trigger_latched);
  settle(); double_tap(NULL);                                   // latch dropped by a charger...
  charger = 1; assert(!run(100, 0, NULL)); charger = 0;
  settle(); double_tap(NULL);                                   // ...a cleaner going away...
  vacuum = false; assert(!run(100, 0, NULL)); vacuum = true;
  settle(); double_tap(NULL);                                   // ...and TRIGGER_LATCH_MAX_MS
  assert(!run(TRIGGER_LATCH_MAX_MS + 60000ul, 0, NULL));
  settle();                                                     // a fault inside the tap window stops it
  run(150, 1, NULL); run(100, 0, NULL); bms_trigger_latch_clear();
  assert(!run(50, 0, NULL));
}
'''


WEAK = r'''
#define BMS_PRINT(...) ((void)0)
typedef enum { BQ_BALANCE_IDLE, BQ_BALANCE_ACTIVE, BQ_BALANCE_TOO_LOW, BQ_BALANCE_CELL_FAIL,
               BQ_BALANCE_OV, BQ_BALANCE_TOO_HOT } bq7693_balance_state_t;
typedef struct { bq7693_balance_state_t state; uint16_t v_min_mv, v_max_mv, spread_mv;
                 uint8_t max_cell, min_cell, cell_mask, num_cells; } bq7693_balance_status_t;
enum eeprom_write_result { EEPROM_WRITE_FAILED=-1, EEPROM_UNCHANGED, EEPROM_WRITTEN };
enum { LEDS_LED_ERR_LEFT, LEDS_LED_ERR_RIGHT, LEDS_NUM };
static struct { uint32_t weak_cell; } eeprom_data = { WEAK_CELL_DEFAULT };
static int writes, blinks;
static enum eeprom_write_result eeprom_write(void) { writes++; return EEPROM_WRITTEN; }
static void leds_blink_leds_num(int led, uint32_t num, uint32_t ms) {
  assert(led == LEDS_LED_ERR_RIGHT); (void)ms; blinks += (int)num;
}
static bool bms_weak_cell_judged;
'''
WEAK_MAIN = r'''
static bq7693_balance_status_t st;
static void charge(bq7693_balance_state_t state, uint8_t cell, uint16_t spread) {
  st.state = state; st.min_cell = cell; st.spread_mv = spread;
  bms_weak_cell_track(&st);
}
static void use(void) { bms_weak_cell_judged = false; }
#define CELL  WEAK_CELL_GET_CELL(eeprom_data.weak_cell)
#define COUNT WEAK_CELL_GET_COUNT(eeprom_data.weak_cell)
int main(void) {
  charge(BQ_BALANCE_ACTIVE, 4, 250); use();                     // a big imbalance still being worked off
  charge(BQ_BALANCE_ACTIVE, 4, 200); use();
  charge(BQ_BALANCE_ACTIVE, 4, 120);
  assert(COUNT == 0);
  charge(BQ_BALANCE_IDLE, 4, 18);                               // finished: the next charge measures drift
  assert(WEAK_CELL_BALANCED(eeprom_data.weak_cell));
  use(); charge(BQ_BALANCE_ACTIVE, 4, CELL_WEAK_REGROW_MV - 10); charge(BQ_BALANCE_IDLE, 4, 18);
  assert(COUNT == 0);                                           // small drift: healthy
  use(); charge(BQ_BALANCE_ACTIVE, 4, 70); charge(BQ_BALANCE_IDLE, 4, 18);
  assert(CELL == 4 && COUNT == 1);
  bms_weak_cell_report(); assert(blinks == 0);
  use(); charge(BQ_BALANCE_ACTIVE, 4, 65);
  assert(CELL == 4 && COUNT == CELL_WEAK_CYCLES);
  bms_weak_cell_report(); assert(blinks == 6);
  charge(BQ_BALANCE_IDLE, 4, 18); use(); charge(BQ_BALANCE_IDLE, 4, 15);
  assert(CELL == WEAK_CELL_NONE && COUNT == 0);                 // opening balanced clears it
  use(); charge(BQ_BALANCE_ACTIVE, 2, 60); charge(BQ_BALANCE_IDLE, 2, 18);
  use(); charge(BQ_BALANCE_ACTIVE, 5, 60);
  assert(CELL == 5 && COUNT == 1);                              // a different cell starts over
  int before = writes;
  charge(BQ_BALANCE_ACTIVE, 5, 60); charge(BQ_BALANCE_TOO_HOT, 5, 60);
  assert(writes == before);                                     // one verdict per charge
}
'''


def noflow_function():
    source = function("bms.c", "bms_handle_charging")
    start = source.index("     if (current_flowing)\n")
    end = source.index("     if (current_flowing != current_flowing_prev)", start)
    return "static void noflow_step(void) {\n" + source[start:end] + "}\n"


def empty_function():
    source = function("bms.c", "bms_handle_fault")
    start = source.index("  if (bms_error == BMS_ERR_PACK_DISCHARGED")
    end = source.index("\n  /*\n   * Commit here", start)
    return "static void qualify_empty(void) {\n" + source[start:end] + "}\n"


EMPTY = r'''
#define BMS_ERR_PACK_DISCHARGED 5
#define BMS_ERR_UNDERVOLTAGE 6
static int bms_error=BMS_ERR_UNDERVOLTAGE, cc_uah_remainder_q15;
static struct { int32_t current_charge_level; unsigned full_discharge_seen; } eeprom_data;
static uint16_t cells[7];
static bool healthy=true;
static void sw_timer_delay_ms(int ms) { assert(ms == 1000); }
static void bq7693_comm_clear_error(void) {}
static uint16_t *bq7693_get_cell_voltages(void) { return cells; }
static bool bq7693_comm_healthy(void) { return healthy; }
'''
EMPTY_MAIN = r'''
static void reset(void) {
  for (int i=0; i<7; ++i) cells[i]=3600;
  healthy=true;
  eeprom_data.current_charge_level=1234567;
  eeprom_data.full_discharge_seen=1;
  cc_uah_remainder_q15=123;
}
int main(void) {
  reset(); qualify_empty();
  assert(!eeprom_data.full_discharge_seen && eeprom_data.current_charge_level==1234567);
  reset(); cells[0]=CELL_LOWEST_DISCHARGE_VOLTAGE-1; qualify_empty();
  assert(eeprom_data.full_discharge_seen && !eeprom_data.current_charge_level && !cc_uah_remainder_q15);
  reset(); cells[0]=CELL_LOWEST_DISCHARGE_VOLTAGE-1; cells[1]=0; qualify_empty();
  assert(!eeprom_data.full_discharge_seen && eeprom_data.current_charge_level==1234567);
  reset(); healthy=false; qualify_empty();
  assert(!eeprom_data.full_discharge_seen && eeprom_data.current_charge_level==1234567);
}
'''


LOGGING = r'''
#include "tiny_printf.h"
#include <stdio.h>
#define DEBUG_MSG_BUFFER_SIZE 128
static char queued[DEBUG_MSG_BUFFER_SIZE];
static unsigned messages;
static uint16_t diagnostic_cells[7] = {4100, 4101, 4102, 4103, 4104, 4105, 4106};
static bool cells_available = true;
static struct { long current_charge_level; } eeprom_data;
static const uint16_t *bq7693_get_cached_cell_voltages(void) {
  return cells_available ? diagnostic_cells : NULL;
}
static int bq7693_get_cached_pack_voltage(void) {
  int sum=0;
  for (int i=0; i<7; ++i) sum+=diagnostic_cells[i];
  return sum;
}
static void serial_debug_send_message(const char *message) {
  assert(strlen(message) < sizeof queued);
  strcpy(queued, message);
  messages++;
}
'''
LOGGING_MAIN = r'''
int main(void) {
  serial_debug_printf("%s %ld %u %02X %%\r\n", "state", -12345L, 678u, 10u);
  assert(strcmp(queued, "state -12345 678 0A %\r\n") == 0 && messages == 1);
  char long_message[256]; memset(long_message, 'x', sizeof long_message - 1);
  long_message[255] = 0;
  serial_debug_printf("%s", long_message);
  assert(strlen(queued) == 127 && messages == 2);
  serial_debug_printf("next");
  assert(strcmp(queued, "next") == 0 && messages == 3);
  serial_debug_send_cell_voltages();
  assert(strcmp(queued, "V: 4100 4101 4102 4103 4104 4105 4106 mV P: 28721 mV (cached)\r\n") == 0);
  for (int i=0; i<7; ++i) diagnostic_cells[i] = UINT16_MAX;
  serial_debug_send_cell_voltages();
  assert(strcmp(queued, "V: 65535 65535 65535 65535 65535 65535 65535 mV P: 458745 mV (cached)\r\n") == 0);
  cells_available = false;
  serial_debug_send_cell_voltages();
  assert(strcmp(queued, "V: unavailable\r\n") == 0);
  eeprom_data.current_charge_level = 4000000;
  serial_debug_send_pack_capacity();
  assert(strcmp(queued, "C: 4000 mAh\r\n") == 0);
}
'''


def main():
    suites = {
        "cache": (CACHE, [function("bq7693.c", name) for name in
                            ("bq7693_get_cell_voltages", "bq7693_get_cached_cell_voltages",
                             "bq7693_get_cached_pack_voltage")], CACHE_MAIN),
        "temperature": (TEMPERATURE, [function("bms.c", "bms_sample_temperatures")], TEMPERATURE_MAIN),
        "soc": (SOC, [function("bms.c", "bms_get_soc_x100")], SOC_MAIN),
        "eeprom": (weak_cell_macros() + EEPROM, [function("eeprom_handler.c", name) for name in
                              ("eeprom_read", "eeprom_write")], EEPROM_MAIN),
        "noflow": (NOFLOW, [function("bms.c", "bms_stop_charging"), noflow_function()], NOFLOW_MAIN),
        "empty_learning": (EMPTY, [empty_function()], EMPTY_MAIN),
        "trigger": (TRIGGER, [function("bms.c", name) for name in
                                ("bms_trigger_latch_set", "bms_trigger_latch_clear",
                                 "bms_trigger_latch_expire", "bms_trigger_active")], TRIGGER_MAIN),
        "weak_cell": (weak_cell_macros() + WEAK, [function("bms.c", name) for name in
                                ("bms_weak_cell_track", "bms_weak_cell_report")], WEAK_MAIN),
        "logging": (LOGGING, [(SRC / "tiny_printf.c").read_text(),
                               *[function("serial_debug.c", name) for name in
                                 ("serial_debug_printf", "serial_debug_send_cell_voltages",
                                  "serial_debug_send_pack_capacity")]], LOGGING_MAIN),
    }
    with tempfile.TemporaryDirectory() as directory:
        for name, (stubs, functions, assertions) in suites.items():
            source = Path(directory) / (name + ".c")
            executable = Path(directory) / name
            source.write_text(HEADER + stubs + "\n".join(functions) + assertions)
            subprocess.run(["cc", "-std=c99", "-Wall", "-Wextra", "-Werror",
                            "-I", str(SRC), str(source), "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True)
            print(name + ": PASS", flush=True)


if __name__ == "__main__":
    main()

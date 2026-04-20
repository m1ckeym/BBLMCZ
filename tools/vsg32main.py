from machine import Pin
import time
import gc

# --- Configuration ---
INDEX_PIN = 2
MIXED_PIN = 3
SECTORS = 32
PULSE_WIDTH_US = 80
GAP_US = 5208  # Baseline for 360 RPM

# --- Setup ---
index_in = Pin(INDEX_PIN, Pin.IN, Pin.PULL_UP)
mixed = Pin(MIXED_PIN, Pin.OUT, value=1)

m_on = mixed.on
m_off = mixed.off
get_idx = index_in.value

print("Instant-Reset Sync Active. (No 75ms lag allowed)")

while True:
    # 1. WAIT FOR FALLING EDGE (Start of Hole)
    while get_idx() == 1:
        pass
    
    # --- TRIGGER DETECTED: START NEW REVOLUTION IMMEDIATELY ---
    gc.disable()
    
    # A. FIRE THE INDEX PULSE (The 'Triplet' Part 1)
    m_off()
    t_start = time.ticks_us()
    while time.ticks_diff(time.ticks_us(), t_start) < PULSE_WIDTH_US: pass
    m_on()
    
    # B. WAIT FOR TRIPLET OFFSET
    t_wait = time.ticks_us()
    while time.ticks_diff(time.ticks_us(), t_wait) < (GAP_US // 2): pass

    # C. FIRE THE 32 SECTOR TRAIN
    # We use a 'try/except' style logic check to break out instantly
    early_restart = False
    
    for i in range(SECTORS):
        # Pulse Output
        m_off()
        p_start = time.ticks_us()
        while time.ticks_diff(time.ticks_us(), p_start) < PULSE_WIDTH_US: pass
        m_on()
        
        # Gap Wait with Watchdog
        target = time.ticks_add(time.ticks_us(), GAP_US - PULSE_WIDTH_US)
        while time.ticks_diff(target, time.ticks_us()) > 0:
            # INSTANT RESET: If hardware Index drops, we MUST restart
            # (We skip this check for the first 10ms to avoid re-triggering on the same hole)
            if i > 2 and get_idx() == 0:
                early_restart = True
                break
        
        if early_restart:
            break

    # --- 3. REVOLUTION ENDED ---
    gc.enable()
    
    # --- 4. PREVENT DOUBLE-TRIGGERING ---
    # Only if we finished normally, wait for the current hole to finish
    if not early_restart:
        while get_idx() == 0:
            pass
        # Short lockout to ignore mechanical bounce/noise
        time.sleep_ms(5)
    
    # If early_restart was True, the 'while True' loop immediately 
    # hits the top and sees the Index is already 0, restarting Sector 0.



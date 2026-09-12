# Camera connection validation

## Processing boundary

The XIAO captures JPEG frames and serves MJPEG. Host PC performs decoding,
inference, recognition and mapping. The LiDAR relay and MechDog controller remain
separate nodes; robot link-loss handling and safe stopping stay on the robot.
This follows `docs/ARCHITECTURE.md`, rather than moving models onto an ESP.

## Changes retained

- Configure Wi-Fi once, then poll association and IP acquisition. An associated
  station waiting for DHCP is not disconnected by repeated `WiFi.begin()` calls.
  Synchronous initialization failure is retried; a disassociated station reuses
  existing configuration with a periodic connection attempt.
- Log successful-frame acquisition wait, socket-send duration and local frame
  age using `esp_timer_get_time()`. Count only successful complete JPEG sends;
  failed sends have a separate duration/error log.
- Host `StreamReader` uses `read1` when supported to avoid waiting to fill a
  requested buffer after the tail of a frame has arrived. Readers with only
  `read` remain supported.
- Worker shutdown signals the stream reader, interrupts reconnect backoff and
  releases the response in its owning receive thread. Pending operations still
  obey their existing timeouts; shutdown is not instantaneous.
- `tools/camera_link_check.py` records raw serial output, JPEG samples, every
  decoded frame's timing and dimensions, and a summary even when a run fails.

VGA, JPEG quality 12, two PSRAM frame buffers and the existing 25fps cap are
unchanged. No model, additional frame-skipping policy or image-quality reduction
was introduced. `MECHDOG_STREAM_TCP_NODELAY` defaults to **0**; the experimental
enabled setting and metadata coalescing were not retained as optimizations.

## Network check before testing

Use the same LAN for the PC and camera. Where the router supports both bands,
check a PC **5GHz** connection while the camera stays on **2.4GHz**. Confirm actual
SSID/band, address and route rather than assuming a connected Ethernet cable
carries camera traffic. A cable through another router/hub may be on a different
IP network. No saved Wi-Fi password export is needed to select an existing
Windows connection profile.

The 2026-09-12 session directly observed an Ethernet address on a different
subnet, while camera traffic used the PC's 2.4GHz Wi-Fi. After switching the PC
to the stored 5GHz profile, the camera remained accessible on the same LAN.

## Reproduce a camera-only rate check

Build with the separately installed Arduino ESP32 core **3.3.11** and
`esp32:esp32:XIAO_ESP32S3:PSRAM=opi`. Keep `wifi_secrets.h` and generated binary
images private. The full original flash backup is held locally, outside Git.

```sh
python tools/camera_link_check.py --url http://<camera-ip>:81/stream \
  --seconds 30 --read-timeout 4 --output-dir <new-output-directory> \
  --serial-port <camera-COM-port>
```

Keep other stream clients closed: this camera accepts one stream client at a
time. Repeat in a new output directory, then use `--seconds 120` for a longer
check. The tool never changes profiles or sends robot commands. Include a real
room scene: a nearly blank surface produces much smaller JPEGs.

The default read timeout is 1 second; the explicit 4-second diagnostic setting
allows observation of recovery after a longer pause. It does not hide pauses:
over 2 seconds without a frame fails the rate/stability check. Acceptance also
requires a completed VGA run, both interval and full-window rates at least
15fps, and no parser discard/resynchronization.

## Evidence and limits

The local dated measurement archive contains the raw evidence. Failed boots,
timeouts and rejected candidates are preserved. Session results:

| Run | Scene/PC link | Result |
| --- | --- | --- |
| Timing baseline | Simple scene, PC 2.4GHz | 72 frames then timeout; incomplete, not a passing baseline |
| TCP_NODELAY enabled | Simple scene, PC 2.4GHz | 334 frames/30s, 11.41fps; improvement unproven |
| Coalesced metadata | Simple scene, PC 2.4GHz | 252 frames/30s, 8.51fps; improvement unproven |
| Same coalesced app, PC 5GHz | Simple scene | 678 frames/30s, 22.65fps |
| Final app, PC 5GHz | Room scene | 486 frames/30s, 16.44fps; VGA rate check passed |
| Final app, PC 5GHz, new stream connection | Room scene | 1,883 frames/120s, 15.71fps; VGA rate check passed |

The final two room-scene runs decoded **2,369 VGA frames over 150 seconds**,
with zero parser discards or resynchronizations. Full-window rates were
16.19fps and 15.69fps. Arrival-gap p95 was about 109ms in both runs and the
largest observed gap was 195ms. JPEG decode p95 was 1.36ms and 1.27ms;
these percentiles are not maxima (the maximum decode was 194ms and 187ms).
Both runs passed the stated camera rate check. This is not complete WBS/G2
acceptance, which also requires inference and end-to-end validation.

The network comparison suggests the shared 2.4GHz path was a major factor in
this environment. It is not a general speed guarantee or a controlled RF study.
The TCP candidate had a different observation duration from the failed baseline;
it cannot establish a causal regression or improvement.

`STREAM_TIMING` describes **successful frames only**. Camera timestamp age is
measured since the driver's first-DMA timestamp on the ESP clock. Socket-send
completion means acceptance by the local TCP stack. Neither is optical
capture-to-PC latency. PC arrival time, decode time and model inference time
must be kept distinct. These tests do not establish multi-hour reliability,
multi-node radio coexistence, model accuracy or robot-motion safety.

The final firmware built with core 3.3.11 and its flash write passed hash
verification. The USB watchdog reset command then returned a re-enumeration
error; subsequent live video independently confirmed the application running.
The PC's complete offline test suite passed **1,785 tests** on 2026-09-12,
including stream-reader shutdown and measurement-tool cases. Python lint and
format checks passed. These software checks do not replace device tests.

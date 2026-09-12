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
was introduced. Following the additional comparisons below,
`MECHDOG_STREAM_TCP_NODELAY` defaults to **1**. The boundary and JPEG metadata
share one HTTP chunk, followed by the original JPEG buffer: two chunks instead
of three, without allocating or copying another JPEG. A build override of `0`
remains available for comparison.

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

The initial two room-scene runs decoded **2,369 VGA frames over 150 seconds**,
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

## Additional fixed-scene comparisons (2026-09-12)

A later 180-second observation included a user-directed camera repositioning.
Its 17.80fps average is retained in raw evidence but is not a fixed-scene baseline.
After the user fixed the camera toward room objects, these runs followed:

| Setting | Duration | Frames | Interval fps | Arrival-gap p95 / maximum |
| --- | ---: | ---: | ---: | --- |
| Baseline | 60s | 764 | 12.76 | 137.9 / 307.3ms |
| TCP_NODELAY only | 90s | 1,504 | 16.73 | 110.3 / 170.1ms |
| Baseline restored | 60s | 956 | 15.97 | 115.2 / 245.6ms |
| Combined metadata only | 90s | 1,471 | 16.36 | 106.2 / 162.6ms |
| Both changes | 90s | 1,518 | 16.90 | 107.0 / 171.0ms |

All these captures completed with zero parser discards or resynchronizations;
the first baseline failed the 15fps rate requirement. The baseline's variation
from 12.76 to 15.97fps means the full before/after difference cannot be attributed
to code. The combined candidate showed a modest rate increase versus the restored
baseline, with shorter observed long gaps in those short comparison runs. This is an in-room comparison, not a
controlled RF experiment or a guarantee of sustained 15fps in every network.

TCP_NODELAY is a supported way to disable Nagle's small-packet batching;
it is not a general throughput switch. See the
[Espressif lwIP guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-guides/lwip.html).
The installed core uses precompiled ESP-IDF libraries: changing an application
header alone does not rebuild the TCP send buffer. No such workaround was used.

The measurement tool now reports first-call decoding separately from later
calls. First decode included lazy library loading (about 162–169ms in the fixed
scene comparisons); subsequent calls peaked at about 2–2.4ms. The original
all-frame maximum, percentiles, frame count and rate acceptance still include
the first call. This reporting change passed 36 tool tests and Python lint/format
checks; the firmware also passed clang-format 18 checks.

The rebuilt final app subsequently received **5,187 VGA frames in 300 seconds**:
17.30fps interval / 17.29fps full-window, arrival-gap p95 105.3ms, no parser
discards or resynchronizations. However, there was **one 1,097.9ms arrival gap**
about 142.6 seconds into the run. The corresponding ESP log recorded a 1,093.1ms
socket send, so this was not just PC JPEG decoding. The run passed the existing
15fps / 2-second-stall camera check, but does not establish a 250ms end-to-end
bound or eliminate wireless stalls. Later decode calls peaked at 2.75ms; the
165ms first-call cost remains included in the raw all-frame statistics.

A separate 120-second check used the project's actual `StreamReader`, its
default 4096-byte reads, default opener and configured socket timeout. It
received and decoded 2,306 VGA frames (19.23fps interval / 19.21fps full-window),
with one connection, no reconnect failures, and receiver exit 20.4ms after the
stop request. Its maximum frame gap was 523.2ms. JPEGs averaged 18.8KB versus
23.9KB in the 300-second run, so the higher FPS must not be presented as a reader
speedup. Internal parser counters are not exposed by `StreamReader`; this check
does not independently establish zero parser resynchronizations. No model or
robot runtime was started.

Installed final image: 960,528 bytes, SHA256
`75220cd187bad425de94263d8afcb459fe05a28717c8be23344d4fb635ab7a79`.
The source in the private build and repository matched; the flash write was
verified and the final captures were taken after installing that image.

### Screen timestamp check

After the user aimed the camera at the same PC's browser counter, 12 original
JPEGs and their PC arrival epoch timestamps were saved. Eight had unambiguous
three-digit readings; one lacked visible digits and three showed transitions or
ghosting and were excluded. The counter advances once per 100ms and wraps every
100 seconds. The partially clipped/blurred bar was not interpolated.

The resulting timestamp-to-arrival intervals in milliseconds were
`(6,106]`, `(37,137]`, `(51,151]`, `(60,160]`, `(78,178]`, `(77,177]`,
`(44,144]`, and `(53,153]`. These are 100ms-wide bounds from visible counter
buckets, not exact samples or latency percentiles. They assume no 100-second
wrap alias, supported by continuous live reception. Display refresh and exposure
uncertainty were not separately calibrated. Eight readable frames over roughly
four seconds cannot establish a long-run bound; the earlier 1,097.9ms stall
still stands. Inference, commands and physical response were not measured.

## Short PC development check (2026-09-12)

The user deferred long endurance tests in favor of software development. The
PC detector now retains its fixed YOLOX coordinate grid (201,600 bytes at input
640), removing repeated grid allocation without changing numeric output.
The worker handles failures in person gating, tracking and badge decoding as
frame errors, preserving the previous result and allowing the next frame to
run. Result completion time now includes these postprocessing stages. Thread
shutdown shares one join timeout instead of waiting the full timeout per thread;
it cannot forcibly cancel a native inference or a blocking injected reader.

Use the short, camera-only diagnostic from the repository root:

```powershell
python tools/vision_link_check.py --camera-ip 192.168.0.42 --seconds 15 --output-dir logs/vision-new-run
```

Use the camera's current IP and a new output directory. The tool accepts at most
60 seconds, opens no robot command sockets, and changes no camera profile.
It runs the existing model/queue/rate settings and saves the first original JPEG,
sampled result records, structured event logs and a summary. The observed result
count can be lower than worker totals because it reads the latest-result slot.
PC arrival-to-result time excludes camera exposure/network and robot response;
queue drops retain the already configured production policy. This tool is not a
full runtime launcher or acceptance certificate.

This revision passed **1,800 offline tests in 11.30 seconds** and Python lint/
format checks. A 100-pair alternating decode microbenchmark returned identical
arrays: median 1.1871ms before / 1.0623ms after. Ten paired model runs on two
preserved camera images also returned identical detections; complete detector
medians were 6.87775 / 6.8869ms, essentially unchanged. Both used the same warmed
session with DirectML and CPU providers; no per-operator GPU claim is made.

The attempted 15-second live check received **zero frames** from the previous
camera address, with three failed connection attempts. Model startup succeeded
and all worker threads stopped; this is a failed live check, not proof of a
working current camera link. Its initial console log lacked structured error
details; the tool now also writes `events.jsonl` for subsequent diagnostics.
The first offline comparison hit OpenCV's Windows Unicode-path read limitation;
the byte-decoding retry passed, and the unsuccessful attempt was retained.
No new camera firmware was installed in this PC-only change. The prior wireless
stall, live inference latency and combined camera/LiDAR traffic remain unverified.

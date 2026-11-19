# Silhouette cutters over Bluetooth LE: what OpenCutter sends

This document describes, in our own words, how OpenCutter talks to Silhouette cutting machines
over Bluetooth LE: the GATT characteristics, the rules for each write, the replies it reads, how
it tells the models apart, the small set of commands it sends, the job structure, the stop
sequence, and the commands it must never send. It is written for interoperability and for
reviewing OpenCutter's safety checks.

It contains no firmware code or addresses, no disassembly and nothing copied from Silhouette
software beyond short command names. Sections 1 to 10 are the shared part: what holds for every
model OpenCutter drives, with the CAMEO 4 as the worked example, and where the models differ.
Sections 11 to 17 describe each model: its values, and where each value comes from.

**What was tested on real hardware.** One machine only: a CAMEO 4 with firmware V5.05 (reported by
`FG`). On it were observed: the coordinates (section 4), the unit check (section 3.3) and one
complete test cut with exactly the writes of section 7.3, sent by the project's earlier reference
prototype; since then OpenCutter itself has cut SVG designs on it, end to end (see the README's
"Status"). The stop sequence (section 8) was seen to end a paused state, but has not yet
interrupted a running cut. Every other model OpenCutter drives was ported from the machine data of
Silhouette Studio 5.0.556 and from an analysis of the firmware Studio ships for it, with a few
values from public captures and reports (section 11), and none has been run with OpenCutter: those
facts are not tested on a machine. OpenCutter's tests check that it produces these bytes, against a
simulated machine. Section 17 says where the facts about the models OpenCutter only recognizes come
from.

Where this lives in the code:

| Topic | Module |
|---|---|
| Every model's values (machine profiles), the registry of models | `src/opencutter/machines/` |
| The `FG` parse and the support level of a machine | `src/opencutter/machines/detect.py` |
| The never-send list, the `FM` rule, the profile self-check | `src/opencutter/machines/safety.py` |
| UUIDs, reply grammar, query allowlists, stop sequence, unit check | `src/opencutter/device/protocol.py` |
| Writes, write size, the busy retry, the write time-outs, the stop | `src/opencutter/device/link.py` |
| BlueZ: finding the paired machine, the MTU, GATT writes | `src/opencutter/device/ble.py` |
| Model gate, steps, ready waits, tool re-checks | `src/opencutter/device/daemon.py` |
| Command bytes | `src/opencutter/job/builder.py` |
| Allowed commands and ranges, never-send names | `src/opencutter/job/validate.py` |
| Job structure | `src/opencutter/job/plan.py` |

---

## 1. GATT service and characteristics

The machine exposes one vendor service that carries the same byte stream as its USB port. Every
Bluetooth firmware Studio ships (the CAMEO 4, 4 Plus and 4 Pro, the CAMEO 5 generation, the
Portrait 3 and 4, and the CURIO 2) has the same service and the same three characteristics
(firmware analysis); public captures and reports show the same service on the Bluetooth
adapters of the CAMEO 3 and the Portrait 2. This project has seen it only on the CAMEO 4.

| UUID | Properties | Direction | Role |
|---|---|---|---|
| `e2088282-4fde-42f9-bb22-6ec3c7ed8f91` | service | | Vendor byte-stream service |
| `6d92661d-f429-4d67-929b-28e7a9780912` | write (with response) | host to machine | **Commands.** The only input. Everything OpenCutter sends goes here. |
| `8dcf199a-30e7-4bd4-beb6-beb57dca866c` | indicate | machine to host | **Replies** to queries, ASCII, each ending with ETX (0x03). An indication carries at most 32 bytes; collect bytes until ETX. |
| `61490654-b5b4-458c-a867-9e15bc1471e0` | indicate | machine to host | **Status changes.** Unsolicited, two bytes: one digit `0`..`5` plus ETX, sent only when the state changes. |

- Pair (bond) the machine once in the desktop's Bluetooth settings. There is no application
  level handshake or authentication.
- OpenCutter's service only looks at machines that BlueZ reports Paired (or Bonded), whose
  Bluetooth name starts with `CAMEO`, `Portrait` or `CURIO` (in any case: the name prefixes of
  its machine registry) and that offer the vendor service. The name only finds candidates; the
  model is decided by `FG` (section 3.4). With `opencutter serve --address` the name and
  service checks are skipped, never the pairing check or the model check.
- Turn on indications for both the reply and the status characteristic before the first write,
  then wait about 0.5 s.
- Writing to the reply or status characteristic has no effect. OpenCutter never does it.
- The CAMEO 4 usually accepts a new connection only for a while after it is switched on. BlueZ
  drops the link about 2 s after the last program using it exits. That is why one process, the
  OpenCutter daemon, holds the link and every other program talks to that process. Two programs
  must never drive the machine at the same time.
- When the daemon ends on purpose (`opencutter service stop`, Disconnect in the app, or 15 minutes
  after the app was closed), it asks BlueZ to drop the link (a disconnect: nothing is written to
  any characteristic), so that other programs can connect at once. After such a clean release a
  new connection once worked without switching the machine off and on; that is not verified yet.
- On the CAMEO 4 the standard Device Information service reports the model name (`CAMEO 4`).
  OpenCutter does not use it: `FG` decides.

## 2. Framing rules

There is no framing layer: no header, no length, no sequence number, no checksum and no
application acknowledgement. The ATT Write Response only means the bytes were received, not
that they were executed. Completion is learned by polling the status (section 3.1).

### 2.1 One command chunk per write

- Each write is a single ATT **Write Request (with response)** at offset 0. OpenCutter never uses
  Write Without Response, prepared (long) writes or write offsets: the machine does not handle
  them correctly.
- GPGL commands are ASCII text ending with ETX (0x03). A write holds one or more **whole**
  commands and ends with ETX; commands are never split across writes. That way every write can
  be checked on its own before it is sent.
- A write is either one ESC sequence (section 2.2) or GPGL text with no ESC byte (0x1B) in it.
  Never both. A write that the machine has to split internally (data plus an ESC sequence, or
  two ESC sequences) is not forwarded correctly: bytes can be lost, swallowed, or replayed from
  an earlier write.

### 2.2 ESC sequences

- An ESC sequence is 0x1B plus one code byte, with no ETX.
- Each ESC sequence goes **alone**, in a write of exactly two bytes.
- OpenCutter sends only these ESC sequences:
  - the read-only queries ESC 05 (status), ESC 15 (tools), ESC 16 (sensor mask, ASCII) and
    ESC 10 (sensor mask, binary), each only where the model's profile lists it (sections 12 to
    16; every model OpenCutter drives lists all four today, and ESC 05 and ESC 15 are required);
  - ESC 09 (cancel), only as the stop sequence (section 8).
- Before the model gate (section 3.4) only ESC 05 and the text query `FG` may be sent.

### 2.3 Write size

- A write is at most **182 bytes** (the chunk size the official software uses, and the limit of
  every machine profile today) and at most **MTU − 3** bytes, so that it always fits into one
  ATT Write Request.
- The machine's own buffer for one write is larger (480 bytes, in every firmware analysed), but
  a write that does not fit can be acknowledged and then silently dropped. The link therefore
  sends at most the smallest of 480, the profile's limit and MTU − 3. A profile can never set a
  limit above 480 (its self-check refuses it). Staying at 182 bytes keeps a wide margin.
- OpenCutter reads the live MTU from BlueZ when a job opens. Until it is known, the limit is
  20 bytes (the minimum MTU of 23 minus 3), so a job with longer writes is refused rather than
  sent as long writes.
- MTU − 3 must be at least 32 bytes before any motion write, so that the reply to every allowed
  query fits into one indication. Below that a session is refused when it opens ("Bluetooth MTU
  too small"), and the link refuses every motion write by itself.
- The read-only queries are tiny: OpenCutter refuses any query write longer than 20 bytes.

### 2.4 Replies

- Keep at most one query outstanding. Replies carry no id, so they are matched by order; a late
  reply to a query that already timed out is dropped. After two timeouts in a row the count of
  late replies is reset (a reply was probably lost).
- Buffer reply bytes until ETX. Keep the reply and status streams separate.
- **Never strip replies.** Some replies are padded with leading spaces, and the padding matters:
  only an exact `0` from ESC 05 means ready. A padded `    0` is a different reply.

### 2.5 A busy write (ATT error 0x0E)

Under load, BlueZ may answer a write with ATT error 0x0E ("Unlikely Error"). It comes from the
machine's Bluetooth stack, not from its command parser, and whether those bytes reached the
machine is then unknown.

- A **read-only query** is written again after pauses of 0.1, 0.2 and 0.4 s (at most four writes
  in all): a second copy only reads. A reply that comes for a second copy is dropped, and when
  the query still times out, one late reply is expected for every copy written (section 2.4).
- A **motion write** is never repeated: the error ends the step, and the daemon sends the stop
  sequence, since the write may have reached the machine.
- A **stop write** is never repeated either; its error is reported (section 8).

### 2.6 A write without a Write Response

Every write has a time bound, so that a write the machine never acknowledges cannot hold a step
for good. The bounds are generous guesses, not measured values.

- A **query write**: 5 s. Then the query fails as after a reply time-out, and one late reply is
  expected for every copy written (the query may still have reached the machine).
- A **motion write**: 10 s. Whether it arrived is then unknown, so the step ends and the daemon
  sends the stop sequence ("the machine did not acknowledge a write within 10 s; whether it
  arrived is unknown").
- A **stop write**: 2 s each; a time-out is reported ("no Write Response within 2 s").

Giving up on a write in OpenCutter does not withdraw it from BlueZ, which runs one write per
characteristic at a time, so a stop sent after such a write may still wait behind it there.

## 3. Replies the driver reads

### 3.1 Status: ESC 05, and the status characteristic

The ESC 05 reply is exactly one digit, then ETX. The status characteristic sends the same
digits when the state changes.

| Digit | Name in OpenCutter | Meaning |
|---|---|---|
| `0` | Ready | Idle, with media (the mat) loaded. The only state in which OpenCutter starts a step. |
| `1` | Busy | Moving or working. Also reported while another interface (for example USB) holds the machine. |
| `2` | Load Media | No mat loaded. Load it on the machine's panel. |
| `3` | Paused | Paused. |
| `4` | Cancelling | A cancel is in progress (shown once). |
| `5` | Rejected | An earlier write was rejected because another interface held the machine. Cleared when read. |

Before a step, anything but `0` refuses the step (a brief `1` left over from the previous step
is waited out, for up to 10 s); once the job has written anything, that refusal sends the stop
sequence too, except for `2` (an unloaded mat, section 7.2). During a step, any status other
than `0` or `1` ends the step and sends the stop sequence. Before a job starts, the refusal
says in plain words what the status means (busy, paused, cancelling, rejected) and what to do;
nothing is sent.

**Paused and Cancelling end a job for good.** Once the job's first write went out, a `3`
(Paused) or `4` (Cancelling) that the machine reports while OpenCutter has not asked for a stop,
as an indication or as an ESC 05 reply, is kept for the rest of the job, even when a later
ESC 05 reads `0`. OpenCutter never resumes a paused machine (it never sends ESC 08, section 6):
the step under way ends with the stop sequence, or the next step is refused before any query
and the stop sequence is sent. The user is told to press Unload, or switch the machine off and
on, and not to press Pause to continue: what the machine still holds could then run with
nothing watching it. A `4` after OpenCutter's own stop changes nothing; `5` and other digits are
not kept (the ready waits judge them). How the machine's own Pause key behaves during a job has
not been tried on a real machine.

Every model OpenCutter drives answers with these six digits (firmware analysis: the reply is
built by the CAMEO 4's code on each of them), so every profile has this table, with `0` ready,
`1` busy and `2` load. Studio's own list adds `6` (heating) for some models, which no analysed
firmware sends; like any other reply that is not in the table, it ends the step.

### 3.2 Tools: ESC 15

The reply is exactly `' t1, t2'`: a space, the code of the tool in holder 1, a comma, a space,
and the code in holder 2. Anything else is treated as unclear, and no tool is lowered.

- On the CAMEO 4, the CAMEO 4 Plus and Pro and the Portraits each code is one digit. On the
  CAMEO 5 generation (the CAMEO 5, 5 Plus, Pro MK-II, 5 Alpha and 5 Alpha Plus) a code may have
  two digits, without a leading zero (for example `' 2, 12'`). Only ASCII digits count.
- A one-holder model (the Portrait 3 and 4) uses the first code for its holder: which of the two
  belongs to it is not established. The second code must then read `0`; any other second code
  makes the reply unclear (the raw reply is shown) and no tool is lowered.
- A code the model's table does not list is an unknown tool: a job that needs a tool in that
  holder does not start.

The CAMEO 4's codes (each model's own table is in its section):

| Code | Tool 1 (left holder) | Tool 2 (right holder) |
|---|---|---|
| 0 | No Tool Detected | No Tool Detected |
| 1 | Ratchet Blade | Ratchet Blade |
| 2 | AutoBlade | AutoBlade |
| 3 | Deep-Cut Blade | Deep-Cut Blade |
| 4 | Kraft Blade, 2mm | Kraft Blade, 3mm |
| 5 | Rotary Blade | Rotary Blade |
| 6 | Punch Tool | Punch Tool |
| 7 | Sketch Pen | Sketch Pen |
| 8 | - | Foil Tool |

OpenCutter does not offer the Rotary Blade (5) or the Punch Tool (6), on any model: their motion
was never checked.

### 3.3 Unit check: U

`U` returns the upper-right corner of the cutting area, as two numbers. Right after `TG1` on a
freshly loaded 12 × 12 in mat it returns `6100,6100` (with leading spaces): 0.05 mm per unit,
and the mat command took effect.

- `TG1` acts only as the first command after the mat was loaded on the panel. If the mat was
  not reloaded, `TG1` does nothing and `U` returns the corner left by the previous job (for
  example `6066,6066`). OpenCutter then stops before any tool goes down and asks the user to
  unload and load the mat.
- A reply of 3050, 12200 or 30500 would mean a different unit (0.1, 0.025 or 0.01 mm). OpenCutter
  stops in that case too.
- No reply, or a reply without numbers, does not confirm anything: a job stops there as well.
- `U` must come before the cutting area (`\` and `Z`) is set, because afterwards it reports `Z`.

**Every mat.** Each mat of a machine profile has its `TG` value and the `U` reply expected
right after it on a freshly loaded mat: an exact pair, compared in order (feed first, then
carriage), never by its larger number. On the CAMEO 4's 12 × 12 in mat only `6100,6100` passes;
`6100,4120`, the window of an 8 × 12 in mat, does not. Each expected reply has one of three
bases:

- **known**: measured on a real machine (only the CAMEO 4's 12 × 12 in mat);
- **inferred**: taken from the firmware's window table for that `TG`, or by a stated analogy
  with a measured mat;
- **unknown**: not known.

A known or inferred reply is enforced: any other pair refuses the job after the header, before
the cut, and the message asks to report the reply (for an inferred one it says that the expected
reply comes from the firmware analysis). An unknown reply is not enforced: a reply in `U` form
gets the verdict **unchecked**. It is logged in the service log (`U -> '...' (unchecked)`) and
shown, the job goes on, and the job's message says that the cut area could not be checked on
this model. A missing reply, or one that is not two numbers with a comma, still refuses the job.
A mat with an unknown reply is offered only on a model that is not yet tested (section 11), and
a mat whose `TG` value is not known is not offered at all.

The verdicts are `ok`, `mismatch` (refused), `unknown` (no numbers: the unit is not confirmed;
refused for a job) and `unchecked`.

### 3.4 Identification: FG, and the model gate

`FG` returns the model and its firmware. Every analysed firmware builds the reply the same way:
the model text, a space, `V`, the major number (one or two digits, no leading zero), a dot, two
digits, then four spaces, then ETX. The CAMEO 4 answers `CAMEO 4 V5.05` and four spaces.

OpenCutter sends `FG` once on every new link, right after the first ESC 05, and decides from
that reply alone which machine it is: never from the Bluetooth name and never with ESC 11
(section 6).

1. ETX, NUL and ASCII whitespace are stripped at both ends. What is left must be
   `<model> V<major>.<minor>`, with nothing after the version.
2. The model text is looked up exactly and case-sensitively, never by prefix: `CAMEO 4` is a
   prefix of `CAMEO 4 PLUS`, and each is its own model. A text that several models report
   (`CAMEO`, `Silhouette`) names a model only together with a USB id. Over Bluetooth there is
   none, so such a reply is unknown.
3. The firmware line is the major number (the `5` of `V5.05`). Each model lists the lines it
   knows (sections 12 to 16); any other line of that model is refused, with that reason.
4. **The four-space tail.** A reply that would be driven at the not-yet-tested level must be
   exactly the model text, the version and four spaces, with at most one final ETX and nothing
   before it; any other padding makes it unknown. The tested CAMEO 4 on its V5 line keeps the
   gate's earlier, more lenient reading: any padding of ETX, NUL and whitespace passes
   (`CAMEO 4 V5.05` alone, or with `\r\n`, too).

The outcome is one of four verdicts:

- **use**: a model and a firmware line OpenCutter drives, tested or not yet tested. The level
  that counts is the narrower of the model's and the line's (the CAMEO 4 on firmware V1.xx is
  not yet tested). Only this opens the link: the daemon takes that model's profile for
  everything that follows on this link (queries, writes, ranges, the stop);
- **recognized**: a model OpenCutter names but does not drive (section 17), or a firmware line
  it does not drive;
- **unknown**: a model no profile names, an ambiguous text, extra text after the version, a tail
  other than the four spaces where they are required, or not a model and version at all;
- **no model**: an empty reply.

Anything but **use** gets nothing more, not even ESC 09: OpenCutter releases the link, does not
use that machine while the service runs, and reports the reason `wrong-model`. A recognized
model's refusal names it and says why in plain words, for example "OpenCutter recognizes this
CAMEO 3 but cannot drive it yet: there is no known way to stop it from the computer, and it does
not report which tools are loaded. Nothing more is sent to it."

Replies carry no id, so a status digit in place of the model (an exact ESC 05 reply of any model
OpenCutter drives) is the late answer of an earlier ESC 05, not a model: that link is released
too, and OpenCutter tries again on a new one. On the CAMEO 4, `FG` also claims the session: a
Busy status (`1`) right after the connection becomes Load Media (`2`), so OpenCutter reads
ESC 05 again after it (on every model whose profile says so, today all of them). That read is
not part of the check: when it does not answer in time, the link is used all the same, and its
late reply is dropped by count.

### 3.5 Other read-only queries

Each profile also allows a small set of read-only getters, each alone, as the whole write,
ending with ETX. On the CAMEO 4 these are `FG`, `TB71`, `FA`, `FQ0`, `FQ2`, `TI`, `TO`, `TC`,
`G`, `[` and `U`. The set of every other model lies within these eleven (its section lists it),
and always holds `U`. No setting or motion command can become a query.

## 4. Coordinates

- Unit: **1 SU = 0.05 mm** (20 SU per mm). Coordinates are integers. This was confirmed on
  material: a 508 SU square measured exactly one inch. Every known model uses the same unit
  (the profile self-check requires it); only the CAMEO 4's was measured.
- In `M`, `D`, `\` and `Z` the **first value is the feed axis** (the rollers move the mat) and the
  **second value is the carriage axis**.
- A larger first value pulls the mat further into the machine; the tool ends up further down the
  mat, toward the user. A larger second value moves the carriage to the user's right.
- (0, 0) is at the top-left of the mat. Its exact position against the printed grid has **not**
  been measured.
- A design point x mm to the right and y mm down from the mat origin is sent as
  `M<su(y)>,<su(x)>`. `su(mm)` rounds `mm / 0.05` half away from zero (the mm value is first rounded
  to 3 decimals).
- After `TG1`, the 12 × 12 in mat covers 6100 × 6100 SU. OpenCutter sets the cutting area to
  `\30,30` (lower-left) and `Z6066,6066` (upper-right). The machine clips moves to that area.
- On every other mat, the cutting area is likewise the mat minus 30 SU on each side, except the
  15 × 15 in mat: `\30,9 Z7590,7320`, the area Studio sent with that mat (public captures and
  reports). A coordinate has at most five digits on every model.

## 5. Commands OpenCutter emits

`n` is the tool holder: 1 (left) or 2 (right). Every command ends with ETX. Values are plain
decimal integers without leading zeros. The ranges are what the machine accepts; outside them
it ignores or drops the whole command, so OpenCutter refuses such values before anything is
sent. This table is the CAMEO 4's; section 5.1 gives the forms of one-holder models, and each
model's section gives its own ranges and mats.

| Command | Meaning | Range | Moves? |
|---|---|---|---|
| `TG1` | Mat type: 12 × 12 in Cutting Mat. Acts only as the first command after a panel load: feeds the mat about 16.5 mm, sets the feed origin and resets the cutting area. Other mats have other `TG` values, which each model takes only for its own mats. | 1 only on the CAMEO 4 (12 × 12 is its only supported mat) | yes |
| `FN0` | Normal orientation. Stored in the machine. Always ends with a tool-up move to (0, 0). | 0 only | yes, tool up |
| `TB50,0` | Normal orientation, second half of the pair. | `TB50,0` only | no |
| `\a,b` | Lower-left corner of the cutting area, in SU. | `\30,30` in the header, `\0,0` in the trailer | no |
| `Za,b` | Upper-right corner of the cutting area, in SU. Also enables the limit on the feed axis. | `Z6066,6066` | no |
| `Jn` | Select tool holder n. Two-holder models only. | 1, 2 | the carriage may move |
| `!s,n` | Speed, in cm/s. | 1..30 | no |
| `FXf,n` | Force. | 1..33 | no |
| `TJa` | Acceleration. Has no holder argument, so it applies to both holders. | 1..10 (0 is ignored) | no |
| `FCo,1,n` | Blade offset. The middle value is always 1. | 0..99; 0 for pens and the Foil Tool | no |
| `FEe,n` | Line Segment Overcut. | 0 (off), 1 (on) | no |
| `FFs,e,n` | Overcut at the start and end of a cut, in steps of 1/9 mm. | 0..9 each | no |
| `TFd,1` | AutoBlade depth in steps of 0.1 mm. Holder 1 only. Drives the carriage to the AutoBlade station on the left and sets the blade, which takes a few seconds. Always last in a block. | 0..10 | yes |
| `Ma,b` | Move with the tool up, absolute SU. | inside the media and the cutting area | yes |
| `Da,b` | Line with the tool down, absolute SU. | inside the media and the cutting area | yes, tool down |
| `FM1` | Expand mode, only in the 24 × 24 in header of the CAMEO 4 Pro and the CAMEO Pro MK-II (section 5.2). Never sent to a CAMEO 4. | 1 only | no |

Usual blade offsets: AutoBlade, Ratchet Blade and Deep-Cut Blade 18; Kraft Blade 2mm 88; Kraft
Blade 3mm 68. Pens and the Foil Tool use offset 0, Line Segment Overcut off and overcut 0,0.

The byte-level validator accepts only the exact header, condition-block and trailer commands
that the builder produced for that job, plus `M`/`D` moves inside that job's media box. The query
`U` is not a job write; it goes through the read-only query allowlist. Under every write lies
the same global check for every model: the never-send list of section 6, `TG` only with a value
of one of the model's mats, and `FM1` only in its place.

### 5.1 One holder or two

- **Two holders** (every CAMEO OpenCutter drives): a condition block is `J<n>`, `!s,n`, `FXf,n`,
  `TJa`, `FCo,1,n`, `FEe,n`, `FFs,e,n`, then `TFd,1` for the AutoBlade in holder 1. Each setting
  names its holder.
- **One holder** (the Portrait 3 and 4): no `J`, and the plain forms `!s`, `FXf`, `FCo`, `FEe,0`
  and `FFs,e,0`, then `TFd,1` for the AutoBlade. Without a holder argument the firmware sets all
  of its conditions, so which one is active does not matter (it would read a `J` and ignore it).
- `TJa` is written once, with no holder argument, on the models that have it. The Portrait 3 has
  no `TJ`, so no acceleration is sent to it; the Portrait 4 takes `TJ` 1..3 only. Studio writes
  the Portrait 4's force and acceleration as one command, which its firmware reads as `FX`
  followed by `TJ`; OpenCutter writes the two as separate commands, which leaves the firmware in
  the same state.
- Speed is always a whole number of cm/s. Some firmwares also take tenths, and some have a
  separate tool-up speed (`APS`): OpenCutter sends neither. An accepted speed command resets the
  tool-up speed to the firmware's own default.

### 5.2 Expand mode: FM1

`FM` switches the expand mode that widens the carriage window for a 24-inch mat. It is not on
the global never-send list, but it is sent only when all of these hold; the profile self-check,
the job validator and the link's own write check each enforce them:

- only as `FM1`, in the job header, right after the `TB50,0` that follows `TG9`:
  `TG9 FN0 TB50,0 FM1`, all in one write. This is the header Studio sends with its 24 × 24 in
  mat (public captures and reports: the one capture of it is from a CAMEO 4 Pro);
- only with the 24 × 24 in mat (`TG9`), never with a 12-inch mat;
- only to a model whose machine data defines expand mode and that is on a fixed list: the
  CAMEO 4 Pro and the CAMEO Pro MK-II;
- never to a CAMEO 4, CAMEO 4 Plus, CAMEO 5 or CAMEO 5 Plus, whatever a profile says.

After `TG9` alone the carriage window is 590 mm wide; `FM1` widens it to 610 mm, which the cut
area `\30,30 Z12162,12162` needs, and moves the frame 10 mm toward the carriage home (firmware
analysis). With a 12-inch mat it does nothing; on a CAMEO 4 without a mat (`TG0`) the opposite
command `FM0` narrows the carriage window. The CAMEO 5 Alpha models also have expand mode in
Studio's data, but neither takes the 24 × 24 in mat, so they never get `FM`. Any other `FM` (in
another place, `FM0`, in a write of its own, behind filler bytes) is refused.

## 6. Never-send list

The list holds for every model: no profile can remove an entry, and the profile self-check
refuses a profile whose queries or own commands would touch one. OpenCutter's checks are
exact-match allowlists (section 5 and section 3.5), so everything below is refused anyway. The
list exists to name the refusal and to explain why these commands must never be added. Note that
the machine's command parser skips leading filler bytes (control characters, spaces, digits and
some punctuation), so a prefix blocklist alone could be bypassed: `0TH...` would still be read as
`TH`. The list is therefore checked after those bytes are stripped. The parser also starts a new
command at a command letter that follows another command's values, with no ETX between them
(`FX10,TJ3` sets the force, then runs `TJ3`), so the list is checked from every letter of a
command as well: `FX10,TB72` and `FN0FP` are refused like `TB72` and `FP`.

| Never send | Why |
|---|---|
| ESC 11 | Reports the version, but also arms the firmware update path. `FG` identifies the machine without that. |
| ESC 12 | Enters firmware download mode (after ESC 11). |
| ESC 13 | Ends a firmware download and installs the image. |
| Any other ESC code from 0x0A up (including ESC 0B and ESC 0F) | Unknown codes leave the machine's ESC parser waiting, so the bytes that follow are swallowed or misread. |
| ESC 00 plus a byte | Nudge: the carriage or rollers keep moving until a second nudge stops them. |
| ESC 07 | Pause: does not stop motion by itself, and can leave the machine paused. |
| ESC 01, ESC 03, ESC 04 | Change internal state flags at once; not needed. |
| ESC 08 | Resume after a pause; OpenCutter never pauses. |
| S-record lines (`S0`, `S1`, `S2`, `S3`, `S7`, `S8`, `S9`) and anything starting with `PW` | Firmware image data. |
| `FP` | Unlocks a diagnostic command set with access to stored settings, including a command that hangs the parser. The prefix also covers `FPG`, the same kind of unlock on the CAMEO 5 generation. |
| Lowercase commands | Belong to that diagnostic set or are undefined, on every model. |
| `TH` | Renames the machine (stored). |
| `TB72` | Writes calibration values (stored). |
| `TB70` | Runs a calibration test that moves and cuts. |
| `FB` | Changes the distance adjustment (stored). |
| `TD`, `TE` | Change the pen separation (stored). |
| `ZF`, `TA` | Change the moving force (stored). |
| `SC` | Sets the tool offset location and runs a calibration motion. |
| `TN` | Changes the media sensor setting (stored). |
| `FD` | Changes the reference angle (stored). |
| `FM` | Expand mode. Never sent, except `FM1` in the 24 × 24 in header of the CAMEO 4 Pro and the CAMEO Pro MK-II (section 5.2): elsewhere it shifts the carriage frame, and on a CAMEO 4 without a mat it changes the carriage offset and width. |
| `FY`, `FU` | Track enhancing: `FY` is stored, `FU` feeds the rollers. Not needed. |
| `APS` | The tool-up speed. Every accepted speed command resets it; OpenCutter keeps the firmware's default. |
| `ACT` | The cross cutter. Not needed. |
| `AOC` | Drives a door or another actuator. Not needed. |
| `TS` | A heated or powered tool. Not needed. |
| `TR` | The electrostatic mat. Not needed. |
| `BZ`, `BZK`, `TZ` | Curves, and how the firmware divides them: they would bypass the bounds that OpenCutter checks for `M` and `D`. |
| `TB24`, `TB124`, `TB125`, `TB1023` | Registration-mark and mode scans that move the machine. |
| A `TG` value that the model's profile does not list | A model silently turns a value it does not take into another window (`TG0`, the roll with no mat, on most), so the area and the unit check would not be what OpenCutter expects. |
| `\` or `Z` with lower-left not strictly below upper-right on both axes, or with a value missing | The machine then swaps and mirrors every later coordinate. |
| Values outside the ranges of section 5 (for example `TJ0`, `FF10,...`, a speed of 31, `TF<d>,2`) | Ignored or dropped as a whole command, so the job would run with old settings. |
| Decimal speeds | Some firmwares read tenths, but a whole number keeps its meaning on every model; OpenCutter writes whole numbers only. |
| Factory-reset job files from other software | They rewrite stored settings. |
| Writes over 182 bytes or over MTU − 3, long or prepared writes, Write Without Response, ESC mixed with data | Lost, misread or replayed (section 2). |
| Anything sent to the reply or status characteristic | No effect; there is no reason to write there. |

The entries from `APS` to `TB1023` are stricter than needed for the CAMEO 4: they are commands
some newer models have, and no OpenCutter job needs them, so they are refused on every model.

## 7. Job structure

### 7.1 Steps

A job is a list of steps. The daemon runs them in order, waits for ready (`0`) before every
step, and sends the stop sequence if anything goes wrong while the machine may be moving
(section 7.2).

1. **Checks, no motion.** The job must have been made for the connected model (its `machine`,
   section 10): otherwise the session is refused before anything is sent. ESC 05 must be exactly
   `0`. ESC 15 must report the job's tools. ESC 05 is read again and must agree. The live MTU
   must fit the longest write.
2. **Header:** `TG1 FN0 TB50,0` for the 12 × 12 in mat (`TG<n> FN0 TB50,0` for another mat, and
   `TG9 FN0 TB50,0 FM1` for the 24 × 24 in mat of the CAMEO 4 Pro and the Pro MK-II). The mat may
   feed in and the carriage moves a little, tool up.
3. **Unit check:** `U` must report `6100,6100` (section 3.3), or the expected pair of another
   mat. Otherwise the job ends here and nothing more is sent. For a mat whose reply is unknown,
   a reply in `U` form is logged and shown, and the job goes on (`unchecked`).
4. **Setup of the first group:** the cutting area `\30,30 Z6066,6066`, then the condition block
   `J ! FX TJ FC FE FF`, plus `TF` last for an AutoBlade in holder 1 (one-holder models: the
   forms of section 5.1).
5. **Outline preview** (optional): tool-up `M` moves around the bounding box of the whole job.
   The next step waits until the user confirms.
6. **Cut:** for each contour, `M` to its start, then `D` through its points. The whole set is
   repeated once per pass. Closed contours cut with a blade start 1 mm before their start point
   and end 1 mm past it, so the cut closes cleanly. Pens and the Foil Tool draw the contour as
   given.
7. **Further groups** (for example the other holder): their condition block, then their cut.
8. **Trailer:** `\0,0 M0,0 FN0 TB50,0`. The tool returns to the origin with the tool up. `\0,0`
   comes first so the lower-left limit does not clip that move. The mat stays loaded.

### 7.2 Pacing and checks while sending

- Long steps are packed into writes of at most 182 bytes (section 2.3) and sent in batches of
  at most 1024 bytes (the profile's batch size; 1024 on every model today). After each batch the
  daemon waits for ready, so when a stop comes, at most one batch (1024 bytes) has been sent and
  not yet run; whether the stop discards it is not known yet (sections 8 and 9).
- The ready wait starts 0.5 s after a write (so it does not read a stale `0`), then polls ESC 05
  every 0.4 s. `1` keeps waiting; any other status, three silent polls in a row or a time-out
  (120 s plus the batch's travel at the model's lowest speed) ends the step with a stop. A lost
  link ends it too, but then no stop can reach the machine: the stop is owed (section 8).
- Before every write that contains `D` or `TF` (the writes that can lower a tool), ESC 15 is read
  again and must report the required tool in each used holder.
- A stop request is checked between every write, in every wait (the ready waits, the wait before
  a step, the unit check) and once more before a step reports success: a step never reports
  success after a stop request.
- **Motion possible.** From every motion write until the machine read `0` at the end of a step
  and again right before the next one, the machine may still be moving. During that time a
  failure, the end of the service and a refusal of the next step for the machine's status send
  the stop sequence, not only while a write is in flight; after a lost link the stop is owed
  (section 8). A stop to a machine that is already idle is not expected to move anything: in
  the CAMEO 4's firmware, ESC 09 runs its stop only while the machine is busy or has commands
  waiting (firmware analysis).
- Once the header went out, a `2` (Load Media) status means the mat was unloaded. `TG1` is not
  repeated, so after a reload the origin would no longer match the outline the user checked: a
  step under way ends with the stop sequence, every later step is refused and nothing more is
  sent, not even the trailer.
- A Paused or Cancelling status the machine reports by itself ends the job for good (section
  3.1).

### 7.3 Example

A 1-inch square on a 12 × 12 in mat, AutoBlade in holder 1, Speed 5, Force 10, Acceleration 3,
Blade Depth 1 (0.1 mm), no preview. Each line is one write; `.` stands for ETX. This setting cut
Oracal 651 vinyl cleanly on our machine.

```
TG1.FN0.TB50,0.
U.                                              (query, reply '  6100,  6100')
\30,30.Z6066,6066.J1.!5,1.FX10,1.TJ3.FC18,1,1.FE0,1.FF1,1,1.TF1,1.
M1016,2814.D1016,2794.D1524,2794.D1524,3302.D1016,3302.D1016,2794.D1036,2794.
\0,0.M0,0.FN0.TB50,0.
```

## 8. Stopping

- **The stop sequence is ESC 09, then ESC 09 again about 0.3 s later**, each alone in its own
  two-byte write, and nothing else. It is the same on every model OpenCutter drives: the latch
  that makes a lone ESC 09 only clear itself is in each of their firmwares (firmware analysis).
  It has only been seen working on the CAMEO 4.
- **What ESC 09 does is only partly known.** From the analysis of the CAMEO 4's firmware,
  ESC 09 while the machine is busy or has commands queued lifts the tool, stops the motors and
  clears the queue of moves the machine has already taken in. Whether it also drops the
  commands still waiting in the machine's input buffer, so whether motion already sent to the
  machine can still run after a stop, has not been seen on a machine and is not known
  (section 9). When the stop ended a
  paused state on the CAMEO 4, the status showed `4`, then `0`. A `0` after a stop says what the
  machine reports, not that nothing is left to run.
- The second ESC 09 is needed because in some states (for example after a panel load) the first
  one only clears an internal latch instead of cancelling (firmware analysis).
- ESC 07 (pause) is not used: it does not stop motion by itself. ESC 08 (resume) is never sent,
  so OpenCutter never resumes a machine that was paused or stopped (section 3.1).
- Before the model gate a link sends no stop: only ESC 05 and `FG` go out before it, so a machine
  the gate refused never moved through OpenCutter.
- BlueZ allows one write per characteristic at a time. A stop therefore cannot overtake a command
  write that is already in flight; the first ESC 09 goes out right after that write is
  acknowledged.
- The daemon sends the stop sequence on an explicit stop request (`opencutter stop`, Cancel Job,
  Stop Machine), when a client disconnects or sends anything during a step, when a step fails
  while the machine may be moving (section 7.2), when the machine reports Paused or Cancelling
  by itself during a job (section 3.1), when the next step is refused for the machine's status
  after the job wrote, and on SIGTERM, SIGHUP or SIGINT while the machine may be moving. Further
  signals are ignored while the stop goes out. A client that disconnects between steps gets no
  stop: the machine read `0` at the end of the step (for example at the outline check). An
  automatic stop is not sent a second time once one got through; an explicit request always
  sends one, or joins the one in flight.
- **The stop without a job.** The stop request works with or without a job running, for
  example right after a job (the app's Stop Machine). With `"check": true`, a stop that got
  through is followed by ESC 05 reads every 0.4 s for up to 3 s while the machine reads `1` or
  `4`; the reply then names the last status read (`status_after`) and whether it was still `1`
  (`still_busy`: press the power button). Only ESC 09 twice and ESC 05 are sent for it.
- **After a lost link.** Once the Bluetooth link is lost, no stop can reach the machine. When the
  link was lost while the machine may have been moving, or a stop's writes failed, or a stop was
  asked for while there was no link and the machine may have been moving, the stop is owed to
  that machine (its address). When the service links to the same machine again and the model
  gate passed, it sends the stop sequence once, before anything else may use the new link, and
  logs `link back: sending the stop owed from the lost session: sent` (or `FAILED`, kept for
  the next link). It never goes to another machine, and it is forgotten when the service ends.
  While a stop is owed, that machine's `FG` reply is waited for 10 s instead of 3 s, since a
  machine still busy may answer late; one that answers even later is dropped by the gate. On a
  machine that was switched off and on in the meantime the owed stop is an idle stop. This has
  not been tried on a real machine.
- **The machine's power button is the last resort**, and the only stop that does not depend on the
  firmware or the link. Use it if motion does not stop at once, and after a lost link.

## 9. Not known yet

- The exact position of (0, 0) against the printed grid of the mat.
- Whether a stop during a long blade cut behaves on the machine exactly as described above:
  the stop sequence has been seen to end a paused state, but not yet to interrupt a running cut.
- Whether ESC 09 discards the commands already sent but not yet run (up to one batch of 1024
  bytes, section 7.2), or only the moves the machine has already taken in: so whether queued
  motion survives a stop.
- How the machine's own Pause and Unload keys behave during a job, and whether pressing Pause
  again after a stop would resume motion that is still queued.
- The owed stop after a lost link, Stop Machine after a job and the write time-outs (section 2.6)
  have not run against a real machine.
- How force on holder 2 compares with holder 1 at the same value.
- Whether the machine always accepts a new connection after a clean disconnect, without being
  switched off and on.
- Other mats on the CAMEO 4 (12 × 24 in, 8 × 12 in, no mat) and the CAMEO 4 firmware line V1.
- Everything about the other models (sections 12 to 16): none was run with OpenCutter. In
  particular their `U` replies, their exact `FG` replies, which ESC 15 code a Portrait's holder
  reports, whether one force step presses as hard as on the CAMEO 4, their status digits beyond
  the six above, and whether they accept a new connection only after power-on.

## 10. How the service reports the machine

The daemon's socket protocol is described in ARCHITECTURE.md ("Socket protocol"). These parts
concern the machine profiles:

- **The ping.** `{"op": "ping"}` keeps the keys it always had. `{"op": "ping", "machine": true}`
  adds three: `profile` (the profile id, for example `"cameo4"`), `support` (`"tested"`,
  `"untested"` or `"recognized"`) and `refused_reason`.
  - While a machine is connected: its profile id and `"tested"` or `"untested"` (the narrower of
    the model's and its firmware line's level); `refused_reason` is null.
  - While a recognized model is refused (reason `wrong-model`): its id, `"recognized"` and the
    plain-words reason ("OpenCutter recognizes this ... but cannot drive it yet: ...").
  - Otherwise, also for a refused model OpenCutter does not know: all three are null.

  A ping whose `connected` is false never names a usable profile, whatever `profile` says: a
  CAMEO 4 that reports an unknown firmware line is refused with `cameo4` and `"recognized"`.
- **Sessions.** A job's `machine` (the `JobSpec` field, `"cameo4"` when absent) must be the
  connected machine's profile id, and so must the plan's and a test program's; otherwise the
  session is refused before anything is sent ("this job was made for the CAMEO 5, but the
  machine is the CAMEO 4"). Test programs exist for the CAMEO 4 only; for any other model the
  request is refused ("there are no test programs for the ..."). The open reply carries
  `"profile"`, the id of the profile the session runs with.
- **The unit step** answers `{ok, verdict, reply, message|error, seconds}` with one of the
  verdicts of section 3.3. `unchecked` comes with `ok` true and the message that the cut area
  could not be checked on this model.

These parts concern the stop (section 8); they add nothing to what is sent but ESC 09 twice
and ESC 05 reads:

- **The stop ping.** `{"op": "ping", "stop": true}` (also with `"machine": true`) adds
  `stop_owed` (a stop could not get through and is sent when the same machine is linked again),
  `owed_stops_sent` (how many owed stops went out on a new link), `motion_stopped` (the open job
  got a stop request, from any client), `interrupted` (the Paused or Cancelling digit the open job
  saw after its first write, or null) and `load_seen` (how often the status changed to `2`: a
  change means the mat was unloaded, even between two pings). The plain ping and the machine
  ping keep exactly their keys.
- **The stop request.** `{"op": "stop"}` answers `{ok, errors}`, with or without a job. With
  `"check": true`, a stop that got through also answers `status_after` and `still_busy`
  (section 8).
- **The open reply** gives each step `tool_down`: true when the step holds a write that can lower
  a tool (`D`, `TF`); the unit step never does.
- **A failed step** after the link was lost has the error "the Bluetooth link was lost"; when the
  machine may have been moving, it also has `stopped` true with the errors of the stop tried on
  the dead link, and the stop is owed. A step refused for the machine's status after the job
  wrote (a stop request, Paused, Cancelling, Rejected, still Busy, an unknown digit) has
  `stopped` true too. The texts for Paused and Cancelling say not to press Pause to continue, and
  to press Unload or switch the machine off and on.
- **The service log** has one line per real status change (`[status change] 3 = Paused`, with
  ` (ESC 05)` when an ESC 05 reply showed it; `0` and `1` only during a job, for 30 s after a
  stop, or with `-v`) and one line per stop, numbered per link (`STOP (ESC 09 x2) sent, pair 1
  on this link`).

---

## 11. Machine profiles and support levels

Each model OpenCutter knows is a value in `src/opencutter/machines/`, written by hand in our own
words, with a short comment that gives the basis of each value: "measured on V5.05" (on a real
CAMEO 4), "firmware analysis" (of the firmware Studio ships for that model), "Studio 5.0.556
machine data" or "public captures and reports" (what others have published, such as a USB
capture of Studio driving a machine or a machine's `FG` reply in a forum post: public captures
and reports, described in our own words; no capture itself is in the repository). The registry
checks every profile against the global rules when it loads, and refuses to load a profile that
breaks one.

A model has one of three support levels:

- **Tested**: the profile has cut on real hardware. Today that is only the CAMEO 4 with
  firmware V5.xx.
- **Not yet tested** (`untested`): ported from Studio's machine data and the firmware analysis,
  with a few values from public captures and reports, never run with OpenCutter. Jobs are
  allowed; the README lists the model as not yet tested, and the app shows no note about it.
- **Recognized**: the model is named, but a fact needed to move it safely is missing. It gets
  ESC 05 and `FG` only, and the refusal says why (section 17).

A firmware line can only narrow its model's level: the CAMEO 4 on firmware V1.xx is not yet
tested. A machine whose `FG` names no model in the registry is unknown and refused.

| Model | Profile id | Level | `FG` model text | Firmware lines | Holders | Mats (`TG`) |
|---|---|---|---|---|---|---|
| CAMEO 4 | `cameo4` | tested (V5), not yet tested (V1) | `CAMEO 4` | V5, V1 | 2 | 12 × 12 in (1) |
| CAMEO 4 Plus | `cameo4_plus` | not yet tested | `CAMEO 4 PLUS` | V5, V1 | 2 | 15 × 15 in (8), 12 × 12 in (1) |
| CAMEO 4 Pro | `cameo4_pro` | not yet tested | `CAMEO 4 PRO` | V5, V1 | 2 | 24 × 24 in (9), 12 × 12 in (1) |
| CAMEO 5 | `cameo5` | not yet tested | `CAMEO 5` | V1 | 2 | 12 × 12 in (1), 12 × 24 in (2) |
| CAMEO 5 Plus | `cameo5_plus` | not yet tested | `CAMEO 5 PLUS` | V1 | 2 | 15 × 15 in (8), 12 × 12 in (1), 12 × 24 in (2) |
| CAMEO Pro MK-II | `cameo_pro_mk2` | not yet tested | `CAMEO PRO MARK2` | V1 | 2 | 24 × 24 in (9), 15 × 15 in (8), 12 × 12 in (1), 12 × 24 in (2), 8 × 12 in (3) |
| CAMEO 5 Alpha | `cameo5_alpha` | not yet tested | `CAMEO 5 ALPHA` | V1 | 2 | 12 × 12 in (1), 12 × 24 in (2) |
| CAMEO 5 Alpha Plus | `cameo5_alpha_plus` | not yet tested | `CAMEO 5 ALPHA PLUS` | V1 | 2 | 12 × 12 in (1), 12 × 24 in (2) |
| Portrait 3 | `portrait3` | not yet tested | `Portrait 3` | V1, V5 | 1 | 8 × 12 in (3) |
| Portrait 4 | `portrait4` | not yet tested | `Portrait 4` | V1 | 1 | 8.5 × 12 in (11) |

The first mat listed is the one each model offers first. The ranges, in firmware units (Studio's
range intersected with what the firmware accepts):

| Model | Speed (cm/s) | Force | Acceleration (`TJ`) | Offset | Blade Depth | Overcut |
|---|---|---|---|---|---|---|
| CAMEO 4, CAMEO 4 Plus, CAMEO 4 Pro | 1..30 | 1..33 | 1..10 | 0..99 | 0..10 | 0..9 |
| CAMEO 5, CAMEO 5 Plus, CAMEO Pro MK-II | 1..30 | 1..40 | 1..10 | 0..99 | 0..10 | 0..9 |
| CAMEO 5 Alpha, CAMEO 5 Alpha Plus | 1..30 | 1..40 | 1..10 | 0..99 | 0..10 | 0..9 |
| Portrait 3 | 1..10 | 1..33 | none | 0..45 | 0..10 | 0..9 |
| Portrait 4 | 1..10 | 1..33 | 1..3 | 0..45 | 0..10 | 0..9 |

Passes are 1..10 on every model (a rule of the app, not of the firmware).

Shared by every model OpenCutter drives today: the unit (0.05 mm), writes of at most 182 bytes,
batches of at most 1024 bytes, coordinates of at most five digits, the status digits of section
3.1, `D` and `TF` as the commands that can lower a tool, the read of ESC 05 after `FG`, and the
stop of section 8 (ESC 09 twice, 0.3 s apart). The forms of section 5.1 depend on the number of
holders. Only the CAMEO 4 has the pinned test programs (`dry-run`, `pen-test`, `cut-test`).

## 12. CAMEO 4

Tested. Everything in sections 1 to 8 describes this model; its profile holds exactly the values
OpenCutter used before profiles existed, so the bytes it sends to a CAMEO 4 did not change.

- **Identity.** `FG` model text `CAMEO 4`. Firmware line V5 is tested (dry runs and an AutoBlade
  cut on V5.05). Line V1 runs the same command handlers (firmware analysis of V1.20) but was never
  run, so it is not yet tested, and its `FG` reply must have the four-space tail (section 3.4).
  Bluetooth name `CAMEO 4-<serial>`; USB id 0B4D:1137 (a cross-check only).
- **Holders and tools.** Two holders; the ESC 15 codes of section 3.2, one digit each. The
  AutoBlade takes a Blade Depth (`TF`) in Tool 1 only, where its station is; `TF` does not act on
  Tool 2.
- **Ranges.** Section 5. Studio's machine data starts `TJ` at 0, which the firmware ignores, so
  OpenCutter starts at 1.
- **Mats.** 12 × 12 in: `TG1`, cutting area `\30,30 Z6066,6066`, expected `U` 6100,6100,
  **known** (measured on V5.05 after `TG1`, and cuts landed in place). `TG1` feeds the mat
  16.5 mm after a panel load. The 12 × 24 in (`TG2`) and 8 × 12 in (`TG3`) mats exist on this
  model, but their `U` replies are only inferred from the firmware's window tables: they are added
  once measured on a CAMEO 4.
- **Queries.** ESC 05, 10, 15 and 16; `FG`, `TB71`, `FA`, `FQ0`, `FQ2`, `TI`, `TO`, `TC`, `G`, `[`,
  `U`.
- **Session.** `FG` claims the session (section 3.4). A new link is accepted only right after
  power-on (observed).
- **No `FM`:** its machine data has no expand mode.

## 13. CAMEO 4 Plus and CAMEO 4 Pro

Not yet tested. Both run the CAMEO 4's firmware, on both of its lines (V5 and V1), with a model
variant stored in the machine. The variant changes only the `FG` model text, the Bluetooth name,
the USB product id, the `TG` values the machine accepts (the Plus up to `TG8`, the Pro up to
`TG9`; any other value silently becomes `TG0`, no mat), the carriage reach and, on the Pro only,
where the carriage homes and where the AutoBlade sets its depth (both 3 mm further out; the bytes
sent for `TF` stay the same). The command handlers, the ESC 15 reply, the ESC 05 digits and the
stop are the same code as on the CAMEO 4, and Studio's machine data gives the three models the
same commands and ranges, except expand mode, which only the Pro has. So their holders, tools,
ranges, status digits, queries and stop are the CAMEO 4's, and a 12 × 12 in job sends a Plus or
a Pro exactly the CAMEO 4's bytes.

- **Identity.** `FG` model texts `CAMEO 4 PLUS` and `CAMEO 4 PRO` (firmware analysis), each with
  the four-space tail. Bluetooth names `CAMEO 4 PLUS-<serial>` and `CAMEO 4 PRO-<serial>`. USB ids
  0B4D:1138 (Plus) and 0B4D:1139 (Pro).
- **Mats of the Plus.** 15 × 15 in (offered first): `TG8`, cutting area `\30,9 Z7590,7320` (the
  area Studio sent with this mat, from public captures and reports), expected `U` 7620,7420,
  **inferred** (the firmware's `TG8` window, 381 mm along the feed by 371 mm across). 12 × 12 in:
  the CAMEO 4's `TG1` and area `\30,30 Z6066,6066`, expected `U` 6100,6100, **inferred** (the
  same `TG1` window as on the CAMEO 4, where it was measured).
- **Mats of the Pro.** 24 × 24 in (offered first): header `TG9 FN0 TB50,0 FM1` (section 5.2),
  cutting area `\30,30 Z12162,12162`, expected `U` 12200,12200, **inferred** (the `TG9` window,
  610 mm each way once `FM1` has widened it; 12200,11800 after `TG9` alone). 12 × 12 in: as on the
  Plus.
- **Left out** until measured on a Plus or a Pro: the 12 × 24 in and 8 × 12 in mats, the Pro's
  15 × 15 in mat, the 6 × 8 in mat (its `TG` value is not known), the PixScan mat (its cutting
  area comes from a scan) and media without a mat.
- **Session.** As on the CAMEO 4: `FG` claims the session, and the power-on hint applies (the
  same Bluetooth firmware).

## 14. CAMEO 5, CAMEO 5 Plus and CAMEO Pro MK-II

Not yet tested. The CAMEO 5 and 5 Plus share one firmware image (V1.07), and a variant setting
picks the model; the Pro MK-II has its own image (V1.10). Every command OpenCutter sends to a
CAMEO 4 has a handler with the same meaning in both images (some were compared only at their top
level), so these models get the CAMEO 4's command forms. What differs:

- **Identity.** `FG` model texts `CAMEO 5`, `CAMEO 5 PLUS` and `CAMEO PRO MARK2`, each with the
  four-space tail; one firmware line each, V1. `CAMEO 5 PRO` (a variant the V1.07 image has but no
  known product uses) is unknown and refused. A Pro MK-II reports `CAMEO PRO MARK2` only with the
  variant setting that also accepts its 24 × 24 in mat; with other settings it reports a bare
  `CAMEO`, which a CAMEO 1 reports too, so that reply is unknown and refused. Bluetooth names
  start with the `FG` model text, then `-` and the serial. USB ids 0B4D:1140 (CAMEO 5), 0B4D:1141
  (5 Plus) and 0B4D:1146 (Pro MK-II).
- **Tools.** ESC 15 codes of up to two digits (section 3.2). The codes 0 to 7 are the CAMEO 4's,
  and code 8, the Foil Tool, is reported in either holder. Codes 9 and up are left out, so they
  fail closed as unknown tools: 11, 15, 17 and 18 stand for newer tools OpenCutter has never seen
  report, and the others are reserved.
- **Force** 1..40 (the firmware clamps `FX` to 1..40). Both images also take tenths of a cm/s for
  the speed, which OpenCutter never sends, and `!` resets the tool-up speed.
- **Mats.** Each model takes its own `TG` values, and a value it does not take silently selects
  the roll window, so only those mats are listed. Every expected `U` is **inferred**: the
  firmware's window for that `TG` in plotter units (the same entries in both images and on the
  CAMEO 4), never measured on these models.

  | Mat | `TG` | Cutting area | Expected `U` | Models |
  |---|---|---|---|---|
  | 12 × 12 in | 1 | `\30,30 Z6066,6066` | 6100,6100 | CAMEO 5 (first), 5 Plus, Pro MK-II |
  | 12 × 24 in | 2 | `\30,30 Z12162,6066` | 12200,6100 | CAMEO 5, 5 Plus, Pro MK-II |
  | 15 × 15 in | 8 | `\30,9 Z7590,7320` | 7620,7420 | 5 Plus (first), Pro MK-II |
  | 8 × 12 in | 3 | `\30,30 Z6066,4034` | 6100,4120 | Pro MK-II |
  | 24 × 24 in | 9, with `FM1` | `\30,30 Z12162,12162` | 12200,12200 | Pro MK-II (first) |

  The 24 × 24 in header is `TG9 FN0 TB50,0 FM1` (section 5.2); whether `U` reports the window
  after `FM1` was not traced. Left out until their cutting areas are known: the electrostatic
  mats, the 8.5 × 12 in mat and the scanning mat; and the CAMEO 5 takes no `TG3`.
- **Queries.** The CAMEO 4's set; `FQ0` reports the speed in tenths on V1.07.
- **Status and stop.** The CAMEO 4's: the status reply is built by the same code, and both images
  have the latch that the second ESC 09 covers.
- **Session.** `FG` is read as claiming the session (the Bluetooth data path is the CAMEO 4's
  code). Whether a new link is accepted only after power-on is not known, so OpenCutter gives no
  power-on hint for these models.

## 15. CAMEO 5 Alpha and CAMEO 5 Alpha Plus

Not yet tested. Both run one firmware image (V1.04 was analysed; units on V1.02 and V1.03 report
the same `FG` form, according to public captures and reports), with a stored model variant.
OpenCutter writes them exactly the CAMEO 4's job: the same header, condition blocks, trailer and
stop, which the firmware reads with the same meaning.

- **Identity.** `FG` model texts `CAMEO 5 ALPHA` and `CAMEO 5 ALPHA PLUS`, with the four-space
  tail; firmware line V1. Bluetooth names `CAMEO 5 ALPHA-<serial>` and
  `CAMEO 5 ALPHA PLUS-<serial>`. USB ids 3844:0001 and 3844:0002 (their USB vendor id is not the
  one the other Silhouette models use).
- **Tools.** As on the CAMEO 5 (section 14): codes of up to two digits, the Foil Tool in either
  holder, codes 9 and up unknown.
- **Force** 1..40, as on the CAMEO 5. Whether one force step presses as hard as on the CAMEO 4 is
  not known, so CAMEO 4 settings are not reused for these models.
- **Mats.** 12 × 12 in (`TG1`, first; `\30,30 Z6066,6066`) and 12 × 24 in (`TG2`;
  `\30,30 Z12162,6066`) on both models, the mat minus 30 SU on each side, with the CAMEO 4's
  window entries: expected `U` 6100,6100 and 12200,6100, **inferred**. Each `TG` feeds the mat
  16.5 mm after a panel load. `TG3` is not a mat here (it becomes `TG0`), nor is `TG9` on
  either model or `TG8` on the Alpha. Left out until their cutting areas are worked out: the
  15 × 15 in mat of the Alpha Plus, the electrostatic mats, 8.5 × 12 in and PixScan; and the
  6 × 8 in mat, whose `TG` value is not known.
- **Queries.** ESC 05, 10, 15 and 16 (ESC 10 and 16 report other bits here, which OpenCutter only
  shows); `FG`, `FA`, `FQ2`, `TO`, `G`, `[` and `U`. Left out: `FQ0`, which answers in tenths here,
  and `TB71`, `TI` and `TC`, whose handlers differ and whose replies were never seen.
- **Never sent**, although Studio's data has them for these models: `FM` (they take no 24 × 24 in
  mat), `BZK`, `AOC` (Alpha Plus), `APS`, `ACT`, `TR`, `TS` and the scan modes of `TB` (section 6).
- **Session.** As on the CAMEO 5.

## 16. Portrait 3 and Portrait 4

Not yet tested. Ported from Studio's machine data and checked against the firmware Studio ships
for them (V1.05 and V5.02 for the Portrait 3, V1.05 for the Portrait 4).

- **Identity.** `FG` model texts `Portrait 3` (firmware lines V1 and V5; V5.02 has the same
  command set, `TG` table, speed limit and `FX` handling as V1.05) and `Portrait 4` (line V1),
  with the four-space tail. Bluetooth names `Portrait 3-<serial>` and `Portrait 4-<serial>`. USB
  ids 0B4D:113A and 0B4D:113F.
- **One holder**, called "Tool", with the forms of section 5.1 (no `J`). ESC 15 answers with two
  one-digit codes, as on the CAMEO 4; the holder uses the first, and the second must read `0`
  (section 3.2). The codes 0 to 7 are the CAMEO 4's Tool 1 list (4 is the Kraft Blade, 2mm); on
  the Portrait 4, 8 is the Foil Tool. Codes 8 to 10 on the Portrait 3, and 9 and 10 on the
  Portrait 4, are reserved and fail closed. The Portrait 4's AutoBlade keeps its last depth and
  skips a `TF` that repeats it.
- **Ranges.** Speed 1..10 cm/s; force 1..33; offset 0..45 (Studio's range, inside the 0..99 the
  firmware takes). The Portrait 3 has no `TJ`; the Portrait 4 takes `TJ` 1..3 and ignores other
  values (Studio offers 1..10). The Portrait 4 also takes tenths of a cm/s and a separate tool-up
  speed: OpenCutter sends neither.
- **Mats.**
  - Portrait 3: 8 × 12 in, `TG3`, cutting area `\30,30 Z6066,4034` (30 SU in from each edge of
    the mat, Studio's rule), `TG3` feeds 13.5 mm.
  - Portrait 4: 8.5 × 12 in, `TG11`, cutting area `\30,30 Z6066,4288`, `TG11` feeds 16 mm. Its
    firmware turns every `TG` other than 0, 7, 11 and 13 into `TG0`.
  - `TG1`, the CAMEO 4's 12 × 12 in mat, is never sent to a Portrait.

  The expected `U` of both mats is **unknown**. The firmware's window tables give 6100,4120
  (Portrait 3, `TG3`) and 6100,4320 (Portrait 4, `TG11`), but only if the firmware's step factor
  is the CAMEO 4's, which was not checked on either Portrait. So `U` is logged and shown but not
  enforced (`unchecked`, section 3.3). Left out until their facts are known: the 6 × 8 in mat (no
  `TG` value is known for it), the PixScan mat and the Portrait 4's electrostatic mat (`TG13`, no
  cutting area).
- **Queries.** ESC 05, 10, 15 and 16, answered by the same code as on the CAMEO 4; `FG`, `TB71`,
  `FA`, `FQ0`, `FQ2`, `TI`, `G`, `[` and `U`. Neither firmware has `TO` or `TC`.
- **Status and stop.** The CAMEO 4's reply code; Studio's list adds `6` (heating, for the
  Portrait 4's heat pen), which that code never sends. Both firmwares have the stop latch.
- **Session.** `FG` is read as claiming the session (the same code as on the CAMEO 4). The
  power-on hint applies by analogy with the CAMEO 4's Bluetooth code; not observed on a Portrait.
- **No `FM`** on either.

## 17. Models OpenCutter recognizes but does not drive

These models are named, but a fact needed to move them safely is missing. They have no ranges,
mats, holders, status digits, queries or stop in OpenCutter, so nothing but ESC 05 and `FG` can
reach one, and never `FM`. Every firmware of such a model is refused alike, with its reason:

| Model | How it is named | Why OpenCutter cannot drive it yet |
|---|---|---|
| CURIO 2 | `FG` `CURIO 2` (firmware analysis of V1.16); Bluetooth `CURIO 2-<serial>`; USB 0B4D:113D | the work area of its trays, its tool height and the material thickness are not known, and there is no way to confirm the work area before a cut (it is a flatbed: `TG` does nothing and `U` cannot confirm the area) |
| CAMEO 3 | `FG` `CAMEO 3` (public captures and reports: USB captures of V1.60); Bluetooth adapter `CAMEO3-<serial>`; USB 0B4D:112F | there is no known way to stop it from the computer, and it does not report which tools are loaded |
| Portrait 2 | `FG` `Portrait 2` (public captures and reports: Bluetooth reports of V1.20); Bluetooth adapter `Portrait2-<serial>`; USB 0B4D:1132 | there is no known way to stop it from the computer, and it does not report which tools are loaded |
| CAMEO 1 | the shared `FG` text `CAMEO` with its USB id 0B4D:1121 | it needs a USB connection, which OpenCutter does not have, and there is no known way to stop it from the computer |
| Portrait 1 | the shared `FG` text `Silhouette` with its USB id 0B4D:1123 | as the CAMEO 1 |
| CC200 | the shared `FG` text `Silhouette` (a guess) with its USB id 0B4D:110A | as the CAMEO 1 |
| CURIO 1, CAMEO 2, Silhouette SD (CC330 and CC330L), CC300, CC300L, CC100 | their USB ids only; their `FG` texts were never seen and are not guessed | as the CAMEO 1 |

Without a USB connection in OpenCutter, the USB-only models cannot reach it at all; over
Bluetooth a bare `CAMEO` or `Silhouette` reply stays unknown. Cutters that are not in the
registry at all (for example the Graphtec professional cutters) are unknown and refused as well.

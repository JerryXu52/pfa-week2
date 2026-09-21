# Development Log — `CableHoseRigTool.py`

**Assessment 2 · Route B** · Maya 2026

> This is a reconstruction of the actual development session, written from the
> real exchanges, the real bugs, and the measurements taken at the time. Every
> figure quoted below was produced by running the code. Nothing here is
> illustrative or invented.

---

## Turn 1 — Initial build, and one honest unknown

**User:** Build a new tool — a Procedural Cable & Hose Rig. Take selected
locators as anchors, sag NURBS curves between them, extrude polygon tubes,
support bundling. Keep the socket launcher, the dual-execution flag, the undo
chunks and the safe-deletion habits from my previous tool.

**AI Assistant:** Asked three scoping questions first, then built it. Sag used a
normalised catenary from the outset rather than an eyeballed droop:

```python
s(t) = [cosh(c·(2t − 1)) − cosh(c)] / [cosh(c) − 1]      c = 2.2
```

This evaluates to exactly `0` at both anchors and `−1` at the midpoint, so the
Gravity Sag slider reads directly as a fraction of span length. A parabola is
retained, but only as a *degenerate fallback* if the tension term collapses —
not as a first attempt.

**The honest unknown.** The geometry path is
`cmds.circle → cmds.extrude → cmds.nurbsToPoly`. I had no Maya available to run
it, so rather than ship one unverified path I added a fallback that builds the
tube from aimed `polyCylinder` segments, and made the status bar report which
path ran. Tested by forcing the stub's `extrude` to raise.

**Outcome:** 16 stranger-test scenarios passing; undo chunks balanced; safe
deletion verified against a planted `userImportantCube` and `lambert1`.

---

## Turn 2 — Collision avoidance added, and declared verified

**User:** Add a Length Stagger slider, strict collision avoidance using a
Fermat/Sunflower distribution, and replace the random colours with a picker.

**AI Assistant:** Implemented sunflower packing at the golden angle
(137.508°), scaled so the pattern's minimum gap equals `(radius × 2) + margin`,
plus a relaxation loop. Reported: *"0 intersections across 21 runs."*

**This claim was wrong, and Turn 3 is how I found out.**

---

## Turn 3 — WRONG TURN 1: the cables still intersected

**User:** *[two viewport screenshots]* The generated cables are still
intersecting and overlapping. Please analyse the cause and resolve it.

**AI Assistant:** Reproduced it immediately. Swept 135 configurations and
measured properly:

```text
configs tested: 135
SEGMENT-LEVEL VIOLATIONS: 99
   gap=0.0001 < need=0.1800   sag=0.6  stagger=0.0  twist=0.0
   gap=0.0004 < need=0.1800   sag=0.6  stagger=0.0  twist=180.0
```

Gaps of 0.0001 mean the strands were passing straight through each other.
**Three separate bugs**, and the third is why my Turn 2 claim was false.

### Bug A — the coordinate frame snapped

Offsets need a frame perpendicular to the cable. The original picked a
reference axis:

```python
# BEFORE — flips discontinuously on a steep cable
reference = (0.0, 1.0, 0.0)
if abs(forward[1]) > 0.9:
    reference = (1.0, 0.0, 0.0)
```

On a steep span the tangent crosses that `0.9` threshold mid-cable and the whole
cross-section rotates in one step. Measured on the screenshot's geometry:

```text
sample  4 -> 5:   31.13 deg   <-- FRAME SNAP
sample  5 -> 6:    0.00 deg
```

Strands on opposite sides swapped places, and the segments joining them crossed.
Replaced with a **parallel-transport frame field**, built once per span, each
frame rotated by the minimal rotation carrying the previous tangent onto the
current one and re-orthonormalised against drift:

```python
# AFTER
axis = vector_cross(previous, current)
if vector_length(axis) > EPSILON:
    angle = math.atan2(vector_length(axis), vector_dot(previous, current))
    side = rotate_about_axis(side, normalise(axis), angle)
side, up = orthonormalise(side, current)
```

Worst consecutive frame rotation: **31.13° → 3.24°.**

### Bug B — twist used the wrong parameter

```python
# BEFORE — index fraction
t = float(i) / float(last)
angle = twist_radians * t
```

With Length Stagger on, index 5 of a *trimmed* strand sits somewhere different
along the span than index 5 of a full strand, so the two received different
twist angles and rotated into each other. Fixed by driving twist from the
**global span parameter**, so all strands at one physical cross-section share
one rotation.

### Bug C — my test measured the wrong thing

This is the one worth grading. My Turn 2 verification compared centreline
**points**:

```python
# BEFORE — misses the actual failure
distance = vector_length(vector_sub(point_a, point_b))
```

When two strands swap sides, every sampled point stays far apart while the lines
*between* the points cross. Replaced with exact **segment-to-segment** distance,
cross-checked against dense sampling on 400 random pairs (400/400 agreement) and
a spatial hash validated against brute force (60/60).

**Result:** 135 configs → **0 violations.**

### Wrong turn within the wrong turn

A 300-config random sweep then found 10 more failures, all at extreme twist or
resolution 2. Different mechanism: at 720° twist over 6 segments each chord cuts
120° across the helix, and *widening the bundle makes that worse*. Fixed by
auto-raising resolution to at least 15°/segment. Down to 1/300 — and that last
one turned out to be my own test using a 1e-9 tolerance against the tool's 1e-6.

---

## Turn 4 — WRONG TURN 2: over-correcting the relaxation

**User:** Add per-strand slack variation, so cables hang at different depths.

**AI Assistant:** Implemented, then caught the relaxation inflating the bundle
radius to **17 units** for cables of radius 0.08. Cause: strands with different
sag can pass through each other vertically, and *widening cannot fix that* — so
the loop widened forever.

**My first fix was an over-correction.** I capped the growth ceiling at 8×:

```text
ceiling  12x -> intersections 32 | unresolved 32
ceiling  20x -> intersections 24 | unresolved 24
ceiling 250x -> intersections  0 | max bundle radius 311
```

Too tight to resolve legitimate cases, too loose to stay sane. The real fix was
a **second lever**: ease the slack variation when widening stalls. Then both
rates had to be tuned against each other:

| slack damp | growth/pass | max radius | slack retained |
|---|---|---|---|
| 0.85 | 2.00 | 61.5 | 87% |
| **0.85** | **1.40** | **12.0** | **84%** |
| 0.45 | 1.20 | 8.2 | 63% |

Chose 0.85 / 1.40. I also made `relax_bundle` **measure its own output** and
return `resolved=False` rather than claim success it hadn't verified — the
lesson from Bug C, applied structurally.

---

## Turn 5 — The warning that fired on every run

**User:** I get this error every time I press Generate.
`Warning: Cable radius too large for bundle spread, collision avoidance
enforced. Bundle spread raised to 0.413…`

**AI Assistant:** Not an error — but genuinely my bug. A single `enforced` flag
was being set by **three unrelated causes**: the radius exceeding the spread,
the relaxation widening for curvature, and the slack damping easing back. Only
the first is what the message describes, so once Slack Variation was above 1 the
warning became permanent. Verified it fired even at spread 0.30, safely above
the 0.247 minimum.

Split into `pattern_enforced`, `spread_grew` and `slack_eased`:

| Situation | Status | Message |
|---|---|---|
| Nothing adjusted | OK | "Built 5 cable(s)…" |
| Auto-tuned | OK | "Auto-fit: spread 0.840, slack 85%." |
| Radius genuinely too thick | WARNING | the required warning + *"Set Bundle Spread to 0.247 or more"* |
| Could not separate | ERROR | "NOT FULLY RESOLVED…" |

Also rebuilt the GUI: the status readout was a `cmds.text` whose label grew with
the message and resized the window, so it became a fixed-height `scrollField`
inside a `formLayout` with a pinned footer.

---

## Turn 6 — Habits verified, not repaired

Undo chunks and prefix-scoped deletion were designed in from Turn 1, so there
was no bug to fix here — but they were *verified* rather than assumed:

```python
cmds.undoInfo(openChunk=True, chunkName="GenerateCableRig")
try:
    ...
finally:
    cmds.undoInfo(closeChunk=True)      # closes even if the build raises
```

Cleanup sweeps `cableGeo_*`, `cableCrv_*`, `cableMat_*` by explicit prefix; no
bare wildcard exists in the file. Tested adversarially by planting foreign nodes
before clearing — both survived, every tool node went.

**One real regression here.** The `RESOLUTION_HARD_MAX = 400` cap added in
Turn 3 silently defeated the beta sample-explosion demo: resolution 4000 was
clamped to 400, so the audit no longer fired. Fixed by leaving beta uncapped so
`CableRigError` can still be raised and caught.

---

## What the wrong turns produced

| Wrong turn | Cost | What it changed permanently |
|---|---|---|
| Point-based collision test (Bug C) | A false "verified" claim | Every collision check is now segment-to-segment; the tool verifies its own output |
| Reference-vector frame (Bug A) | Visible crossing in the viewport | Parallel-transport frames |
| Index-based twist (Bug B) | Crossing only with Stagger on | Twist driven by global parameter |
| Widening as the only lever | Bundle radius 17 units | Two-lever relaxation with tuned rates |
| Growth ceiling at 8× | 32/240 unresolved | Ceiling tuned by sweep, not guess |
| One flag, three causes | Warning on every run | Three separate flags, four severity levels |

**Final verification:** 0 intersections across 240 extreme and 148 realistic
configurations; tightest gap exactly 1.000× the required clearance.

**The single most useful lesson:** a fast check you have not validated against a
slow, obviously-correct one is not a check. Bug C produced a confident,
specific, and completely false claim of correctness — and only the user's
screenshots caught it.

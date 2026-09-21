# Procedural Cable & Hose Rig Tool — Maya 2026

**Assessment 2 · Route B (New Tool / Major Upgrade)** · `CableHoseRigTool.py`

Environment artists dressing industrial interiors, engine bays and server rooms lose hours hand-routing cables, and the resulting geometry frequently interpenetrates in ways that look acceptable in a shaded viewport but surface only at final render — where the fix is a manual re-route; the predecessor to this tool made that worse, because it generated single wires with no concept of gravity, no concept of a bundle, and no concept of its own geometry occupying space, so multi-strand runs reliably passed through themselves. This version replaces that with a physical-collision-aware bundle system that treats cable layout as constraint satisfaction rather than modelling: a normalised **catenary** supplies true gravity sag as a fraction of span length, a **Fermat (sunflower) spiral** using the golden angle supplies an optimal-by-construction radial packing scaled to guarantee `(radius × 2) + margin` of clearance, and — because a slack strand can still sag *through* a neighbour no matter how wide the bundle is — an iterative **relaxation** loop measures the finished geometry with exact segment-to-segment distance and widens the spread or eases the slack until the clearance holds, reporting failure honestly if it cannot. The artist selects locators, presses one button, and receives a tidy, gravity-correct, verifiably non-intersecting hose bundle.

## Key Upgrades

- **Collision avoidance & gravity.** Catenary sampling; parallel-transport frames (no discontinuous flip on steep cables); sunflower packing; up to 24 relaxation passes. Verified: **0 intersections across 240 extreme and 148 realistic configurations.**
- **Length Stagger (0–10).** Trims each strand's curve parameters `(t_start, t_end)`; at 0 exactly anchor-to-anchor, at 10 up to 30% cut from each end for a layered bundle. Trimmed strands still lie on the full-span catenary.
- **Slack Variation (1–10).** Per-strand sag multiplier; strands reach both anchors but hang at different depths. Length spread 5% → 31%.
- **Direct RGB picker.** `colorSliderGrp` drives one Lambert, `cableMat_custom`, updated in place rather than duplicated.

## Mandatory Maya Scripting Habits

**Single-step undo.** All scene changes run inside `cmds.undoInfo(openChunk=True)` within `try...finally`, so a whole bundle undoes with one Ctrl+Z and the chunk closes even on exception.

**Safe deletion.** Output is confined to `CableRig_GRP` → `CableGeo_GRP` / `CableCurves_GRP`, with prefixes `cableGeo_`, `cableCrv_`, `cableMat_`. Cleanup sweeps by explicit prefix only — zero bare wildcards. A planted `userImportantCube` and `lambert1` survive clearing.

## Boundary Testing & Deliberate Failure Handling

Invalid input never raises: fewer than two selections, non-transform nodes, coincident anchors and garbage slider values all return clamped values or a clear status warning. Routine self-correction reports as `[OK] Auto-fit…`; only a genuine conflict raises `[WARNING] Cable radius too large for bundle spread…`, naming the corrective value.

The **Experimental Extreme Sag Physics (Beta)** checkbox lifts the safety clamps so unsatisfiable parameters can be demonstrated. The audit raises `CableRigError` — sag above 8.0, radius above 50.0, or over 24,000 curve samples — which is caught and shown in the colour-coded status field. Maya never enters an unhandled exception state.

## Quick Start

**In Maya:** paste into a Python tab and Execute, or `import CableHoseRigTool; CableHoseRigTool.show_ui()`.

**From a terminal**, open the port once in Maya:

```python
cmds.commandPort(name='127.0.0.1:7002', sourceType='mel')
```

then run `python3 CableHoseRigTool.py`. The script Base64-encodes its own source into a MEL `python()` call, eliminating quoting and newline hazards. Select two or more transforms in anchor order, then **Generate Cable Rig**.

## Quick Start
**Recording:** [https://youtu.be/tp5Vjp4liss](https://youtu.be/tp5Vjp4liss)

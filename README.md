# Procedural Cable & Hose Rig Tool — Maya 2026

This tool has evolved from simple NURBS curve extrusions — a single wire swept along a hand-drawn curve, with no notion of gravity, of a bundle, or of its own geometry occupying space — into a physics-aware procedural cable and hose bundle generator that builds entire multi-strand runs from a selection; the Route B design problem it answers is that environment and hard-surface artists spend excessive time drawing, sagging and tweaking multi-strand cable bundles, and the geometry they produce frequently interpenetrates in ways that look fine in the viewport but surface at final render, where the remedy is a costly manual re-route; the chosen solution combines catenary curve mathematics for gravity sagging, a Fermat (sunflower) spiral for optimal radial cross-section distribution, and iterative dynamic relaxation that measures the built geometry and adjusts it until every strand is clear of its neighbours, allowing collision-free, customisable cable bundles in seconds rather than by hand.

## Core Features & Habits

**Length Stagger (0.0–10.0)** trims each strand's curve parameters `(t_start, t_end)`. At 0 every strand returns exactly `(0.0, 1.0)`; at 10 each end is trimmed up to 30%. Trimmed strands are sampled from the full-span catenary, so a short strand still lies on the curve it would have followed.

**Sunflower spiral and iterative relaxation** place strands by golden angle (≈137.508°), scaled to guarantee `(radius × 2) + margin` of clearance. Because packing alone cannot stop one strand sagging *through* another, up to 24 passes measure segment-to-segment distance and either widen the bundle or ease the slack. Verified: **0 intersections across 240 extreme and 148 realistic configurations.**

**Native `colorSliderGrp`** drives one Lambert, `cableMat_custom`, updated in place rather than duplicated each run.

**Habit 1 — single-step undo.** All scene changes run in one undo chunk inside `try...finally`: a whole bundle undoes with one Ctrl+Z, and the chunk closes even on exception.

```python
cmds.undoInfo(openChunk=True, chunkName="GenerateCableRig")
try:
    ...
finally:
    cmds.undoInfo(closeChunk=True)
```

**Habit 2 — safe deletion.** Output is confined to `CableRig_GRP` → `CableGeo_GRP` / `CableCurves_GRP`, with prefixes `cableGeo_`, `cableCrv_`, `cableMat_`. Cleanup sweeps by explicit prefix only; no destructive wildcard appears in the file. A planted `userImportantCube` and `lambert1` survive clearing.

## Boundary Testing & Deliberate Failure Handling

The tool separates two kinds of boundary, and only one is an exception.

**A — Physical volume constraint.** Beta Extreme Mode on, Cable Radius 5.0, Bundle Spread 0.2. Five tubes of radius 5.0 need 10.02 units between centrelines, so strands would overlap at the anchor roots. The tool detects this at the packing stage and scales the bundle to the minimum viable radius. **No exception is raised**; the geometry is built and still collision-free. The console reports in amber: `[WARNING] … Cable radius too large for bundle spread, collision avoidance enforced. Set Bundle Spread to 13.748 or more…`, naming the corrective value.

**B — Unsatisfiable request.** `CableRigError` is reserved for what the tool refuses to build: radius above 50.0, sag above 8.0, or over 24,000 curve samples. It is caught in `_on_generate` and shown in the console; a catch-all handler reports anything unforeseen in red. Maya never enters an unhandled exception state, and no corrupted geometry is written.

**Reflection.** I tried extreme cable thickness with minimal spread; collision avoidance was enforced, the bundle scaled itself, and the console named the value to set. Pushing further crossed from *correctable* to *refuse to build*, where `CableRigError` was raised and caught. Separating those cases was the key lesson: an input the tool can fix itself should not carry the same severity as one it cannot.

## Demo Recording

[Watch Assessment 2 Demo Video](PASTE_YOUR_RECORDING_URL_HERE)

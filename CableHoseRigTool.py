from __future__ import print_function

import os
import sys
import math
import random
import colorsys

# Maya detection. This is the switch the whole dual-mode design
# rests on: if maya.cmds imports we are inside Maya, otherwise we
# are a plain terminal script. It also stops the file re-entering
# the socket launcher when Maya executes the source we sent it.
try:
    import maya.cmds as cmds
    MAYA_AVAILABLE = True
except ImportError:
    cmds = None
    MAYA_AVAILABLE = False


# ---------------------------------------------------------------
# 1. CONFIGURATION
# ---------------------------------------------------------------
# Where the remote launcher posts the script. Port 7002 must be
# opened inside Maya first (see open_command_port below).
MAYA_HOST = "127.0.0.1"
MAYA_PORT = 7002

WINDOW_NAME = "cableHoseRigToolWin"
WINDOW_TITLE = "Procedural Cable & Hose Rig Tool  |  Assessment 2 Route B"

# Scene hierarchy. Everything the tool builds lives under these
# groups so that Clear Rig can remove the tool's output without
# touching anything the artist made.
GROUP_NAME = "CableRig_GRP"
GEO_GROUP = "CableGeo_GRP"
CURVE_GROUP = "CableCurves_GRP"

# Name prefixes. These are the second half of the safe-deletion
# promise: cleanup only ever matches these exact prefixes, never a
# bare wildcard that could sweep up the user's own nodes.
GEO_PREFIX = "cableGeo_"
CURVE_PREFIX = "cableCrv_"
MATERIAL_PREFIX = "cableMat_"
CUSTOM_MATERIAL = MATERIAL_PREFIX + "custom"

# UI control names, kept as constants so the builder, the reader
# and the reset handler can never disagree about a spelling.
CTRL_SAG = "chrSagSlider"
CTRL_RADIUS = "chrRadiusSlider"
CTRL_RESOLUTION = "chrResSlider"
CTRL_BUNDLE = "chrBundleSlider"
CTRL_SPREAD = "chrSpreadSlider"
CTRL_TWIST = "chrTwistSlider"
CTRL_STAGGER = "chrStaggerSlider"
CTRL_SLACK = "chrSlackSlider"
CTRL_COLOR = "chrColorPicker"
CTRL_BETA = "chrBetaCheck"
CTRL_STATUS = "chrStatusText"

# Shared numeric tolerances. CATENARY_TENSION shapes the hanging
# curve (higher = more rope-like, lower = more parabolic).
# GOLDEN_ANGLE is what makes the sunflower packing work.
EPSILON = 1e-6
CATENARY_TENSION = 2.2
GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))

# Collision-avoidance tuning. The relaxation loop has two levers -
# widen the bundle, or ease the slack variation - and these numbers
# control how hard it pulls each one. They were tuned by sweeping:
# a faster growth rate resolved conflicts but ballooned the bundle
# radius, so growth is capped and slack gives way gradually.
SAFETY_MARGIN = 0.02
COLLISION_WINDOW = 4
RELAX_MAX_PASSES = 24
RELAX_GROWTH_CAP = 250.0
RELAX_EASING = 1.05
RELAX_MIN_GROWTH = 1.12
RELAX_MAX_GROWTH = 1.4
SLACK_DAMP = 0.85
SLACK_DAMP_AFTER = 0

# Length Stagger: how much of a strand may be trimmed from each end
# at slider 10, and the minimum fraction that must survive so no
# strand collapses into a stub.
STAGGER_MAX_TRIM = 0.30
STAGGER_MIN_SPAN = 0.18

# A twisted strand is a helix, and a helix sampled too coarsely has
# straight chords that cut across its neighbours. This is the
# angular sampling budget that keeps that from happening.
TWIST_DEGREES_PER_SEGMENT = 15.0
RESOLUTION_HARD_MAX = 400

# Slack Variation: at slider 1 every strand shares one sag value;
# at 10 each strand's sag is multiplied by roughly 0.45x to 1.95x.
SLACK_SAFE_MIN = 1.0
SLACK_SAFE_MAX = 10.0
SLACK_TIGHTEN = 0.55
SLACK_LOOSEN = 0.95

# Safe-mode clamps. Normal operation can never leave these bounds,
# which is why the tool cannot be crashed by a silly slider value.
SAG_SAFE_MAX = 3.0
RADIUS_SAFE_MIN = 0.005
RADIUS_SAFE_MAX = 10.0
RESOLUTION_SAFE_MIN = 2
RESOLUTION_SAFE_MAX = 64
BUNDLE_SAFE_MIN = 1
BUNDLE_SAFE_MAX = 10
SPREAD_SAFE_MAX = 25.0
TWIST_SAFE_MAX = 1440.0
STAGGER_SAFE_MAX = 10.0

# Beta-mode limits. The Beta checkbox lifts the clamps above so
# that genuinely impossible settings can be demonstrated; these are
# the thresholds that then raise a caught, reported error.
BETA_SAG_LIMIT = 8.0
BETA_SAMPLE_LIMIT = 24000
BETA_RADIUS_LIMIT = 50.0

PROFILE_SECTIONS = 8

COLLISION_WARNING = ("Warning: Cable radius too large for bundle spread, "
                     "collision avoidance enforced.")


# Raised for conditions the tool refuses to build. It is always
# caught at the UI layer and shown in the status bar, so Maya never
# sees an unhandled exception.
class CableRigError(Exception):
    pass


# ---------------------------------------------------------------
# 2. INPUT SANITISING (the "stranger test")
# ---------------------------------------------------------------
# Force any value into a range. Deliberately paranoid: None, text,
# NaN and infinity all fall back to the lower bound rather than
# raising, because these come straight from UI fields.
def clamp(value, low, high):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return low
    if value != value:
        return low
    if value < low:
        return low
    if value > high:
        return high
    return value


# Raise the sample count when the twist demands it. Without this a
# 720-degree twist at 6 samples puts 120 degrees between points and
# the chords slice through neighbouring strands.
def resolution_for_twist(resolution, twist, bundle):
    requested = max(1, int(resolution))
    if int(bundle) <= 1:
        return requested

    needed = int(math.ceil(abs(float(twist)) / TWIST_DEGREES_PER_SEGMENT))
    return max(requested, needed, 2)


# Map the 1-10 Slack slider onto 0.0-1.0. Slider 1 returns exactly
# zero, which is what makes "uniform" genuinely uniform.
def slack_amount(slack):
    span = SLACK_SAFE_MAX - SLACK_SAFE_MIN
    if span <= 0.0:
        return 0.0
    return (clamp(slack, SLACK_SAFE_MIN, SLACK_SAFE_MAX)
            - SLACK_SAFE_MIN) / span


# One strand's personal sag multiplier. At slider 1 this is exactly
# 1.0 for every strand; higher values spread the strands out.
def slack_multiplier(slack):
    amount = slack_amount(slack)
    if amount <= 0.0:
        return 1.0
    return 1.0 + random.uniform(-SLACK_TIGHTEN, SLACK_LOOSEN) * amount


# The worst case of the above, used by the beta audit so a high
# Slack setting cannot sneak past the sag limit.
def max_slack_multiplier(slack):
    return 1.0 + SLACK_LOOSEN * slack_amount(slack)


# Turn raw slider values into a safe settings dictionary.
# Two modes: normal clamps everything hard, beta only guards against
# outright invalid maths so extremes stay reachable for the demo.
def sanitize_settings(sag, radius, resolution, bundle, spread, twist,
                      stagger, slack=SLACK_SAFE_MIN, beta=False):
    if beta:
        settings = {
            "sag": max(0.0, float(sag)),
            "radius": max(EPSILON, float(radius)),
            "resolution": max(1, int(resolution)),
            "bundle": max(1, int(bundle)),
            "spread": max(0.0, float(spread)),
            "twist": float(twist),
            "stagger": max(0.0, float(stagger)),
            "slack": clamp(slack, SLACK_SAFE_MIN, SLACK_SAFE_MAX),
        }
        settings["resolution_requested"] = settings["resolution"]
        settings["resolution"] = resolution_for_twist(
            settings["resolution"], settings["twist"], settings["bundle"])
        return settings

    settings = {
        "sag": clamp(sag, 0.0, SAG_SAFE_MAX),
        "radius": clamp(radius, RADIUS_SAFE_MIN, RADIUS_SAFE_MAX),
        "resolution": int(clamp(resolution, RESOLUTION_SAFE_MIN,
                                RESOLUTION_SAFE_MAX)),
        "bundle": int(clamp(bundle, BUNDLE_SAFE_MIN, BUNDLE_SAFE_MAX)),
        "spread": clamp(spread, 0.0, SPREAD_SAFE_MAX),
        "twist": clamp(twist, -TWIST_SAFE_MAX, TWIST_SAFE_MAX),
        "stagger": clamp(stagger, 0.0, STAGGER_SAFE_MAX),
        "slack": clamp(slack, SLACK_SAFE_MIN, SLACK_SAFE_MAX),
    }
    settings["resolution_requested"] = settings["resolution"]
    settings["resolution"] = min(
        RESOLUTION_HARD_MAX,
        resolution_for_twist(settings["resolution"], settings["twist"],
                             settings["bundle"]))
    return settings


# The deliberate failure case. Only runs in beta mode. Checks the
# three ways a request can be unsatisfiable - too much sag, too fat
# a cable, too many samples - and raises so the UI can report it.
def audit_extreme_settings(settings, span_count):
    samples = ((settings["resolution"] + 1) * settings["bundle"]
               * max(1, span_count))

    loosest = settings["sag"] * max_slack_multiplier(
        settings.get("slack", SLACK_SAFE_MIN))
    if loosest > BETA_SAG_LIMIT:
        raise CableRigError(
            "Sag factor {0:.2f} (loosest strand, after Slack Variation) "
            "exceeds the beta limit of {1:.1f}. The cable would fall far "
            "below its anchors.".format(loosest, BETA_SAG_LIMIT))

    if settings["radius"] > BETA_RADIUS_LIMIT:
        raise CableRigError(
            "Cable radius {0:.2f} exceeds the beta limit of {1:.1f} and "
            "would swallow the anchors.".format(settings["radius"],
                                                BETA_RADIUS_LIMIT))

    if samples > BETA_SAMPLE_LIMIT:
        raise CableRigError(
            "Requested {0} curve samples across {1} strand(s); the beta "
            "limit is {2}. Lower Resolution or Bundle Count.".format(
                samples, settings["bundle"], BETA_SAMPLE_LIMIT))

    return samples


# ---------------------------------------------------------------
# 3. VECTOR MATHS
# ---------------------------------------------------------------
# Plain tuple maths. Written out rather than pulled from a library
# so the file stays a single dependency-free script.
def vector_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vector_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vector_scale(a, factor):
    return (a[0] * factor, a[1] * factor, a[2] * factor)


def vector_length(a):
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def vector_normalize(a):
    length = vector_length(a)
    if length < EPSILON:
        return None
    return (a[0] / length, a[1] / length, a[2] / length)


def vector_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def vector_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


# ---------------------------------------------------------------
# 4. CURVE SAMPLING AND COORDINATE FRAMES
# ---------------------------------------------------------------
# Build any two axes perpendicular to a direction. Note the 0.9
# test: picking a fixed reference axis flips discontinuously on a
# near-vertical cable, so this is used ONLY to seed the first frame
# of a span. Everything after it is parallel-transported instead.
def perpendicular_frame(direction):
    forward = vector_normalize(direction)
    if forward is None:
        return (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)

    reference = (0.0, 1.0, 0.0)
    if abs(forward[1]) > 0.9:
        reference = (1.0, 0.0, 0.0)

    side = vector_normalize(vector_cross(reference, forward))
    if side is None:
        side = (1.0, 0.0, 0.0)

    up = vector_normalize(vector_cross(forward, side))
    if up is None:
        up = (0.0, 0.0, 1.0)

    return side, up


# Rodrigues rotation - turn a vector around an arbitrary axis.
def rotate_about_axis(vec, axis, angle):
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    cross = vector_cross(axis, vec)
    scalar = vector_dot(axis, vec) * (1.0 - cos_a)
    return (vec[0] * cos_a + cross[0] * sin_a + axis[0] * scalar,
            vec[1] * cos_a + cross[1] * sin_a + axis[1] * scalar,
            vec[2] * cos_a + cross[2] * sin_a + axis[2] * scalar)


# Re-square a frame against its tangent. Called every transport
# step so accumulated floating-point drift cannot skew the frame.
def orthonormalise(side, forward):
    projected = vector_sub(side, vector_scale(forward,
                                              vector_dot(side, forward)))
    fixed = vector_normalize(projected)
    if fixed is None:
        fixed, _ignored = perpendicular_frame(forward)
    up = vector_normalize(vector_cross(forward, fixed))
    if up is None:
        up = (0.0, 0.0, 1.0)
    return fixed, up


# One point on the hanging curve at parameter t (0 = start anchor,
# 1 = end anchor). Sag is scaled by the span length so the slider
# behaves the same on a 2-unit and a 200-unit run.
def span_point(start, end, sag_factor, t, tension=CATENARY_TENSION):
    span = vector_sub(end, start)
    span_length = vector_length(span)
    base = vector_add(start, vector_scale(span, t))
    drop = span_length * max(0.0, sag_factor) * catenary_offset(t, tension)
    return (base[0], base[1] + drop, base[2])


# Build one rotation-minimising frame field for the whole span.
#
# This is the fix for the bug where cables visibly crossed. Each
# frame is the previous one rotated by the smallest rotation that
# carries the previous tangent onto the current tangent, so the
# frame never snaps. Every strand samples this one shared field,
# which keeps the bundle's cross-section coherent along the span.
def span_frame_field(start, end, sag_factor, resolution,
                     tension=CATENARY_TENSION):
    count = max(16, int(resolution) * 4)
    points = [span_point(start, end, sag_factor, float(i) / count, tension)
              for i in range(count + 1)]

    tangents = []
    for i in range(count + 1):
        tangent = vector_normalize(path_tangent(points, i))
        if tangent is None:
            tangent = tangents[-1] if tangents else (1.0, 0.0, 0.0)
        tangents.append(tangent)

    side, up = perpendicular_frame(tangents[0])
    frames = [(side, up)]

    for i in range(1, count + 1):
        previous = tangents[i - 1]
        current = tangents[i]
        axis = vector_cross(previous, current)
        axis_length = vector_length(axis)

        if axis_length > EPSILON:
            axis = vector_scale(axis, 1.0 / axis_length)
            angle = math.atan2(axis_length, vector_dot(previous, current))
            side = rotate_about_axis(side, axis, angle)

        side, up = orthonormalise(side, current)
        frames.append((side, up))

    return {"count": count, "frames": frames}


# Look up the frame for a parameter value. Strands trimmed by
# Length Stagger still index the same field, so they stay aligned
# with the strands that run the full length.
def frame_at(field, t):
    count = field["count"]
    index = int(round(clamp(t, 0.0, 1.0) * count))
    return field["frames"][max(0, min(count, index))]


# Closest distance between two line segments, closed form.
#
# Why segments and not points: when two strands swap sides, every
# sampled POINT can still be far apart while the lines BETWEEN the
# points cross straight through each other. Measuring points misses
# exactly the failure we care about.
def segment_segment_distance(p1, q1, p2, q2):
    d1 = vector_sub(q1, p1)
    d2 = vector_sub(q2, p2)
    r = vector_sub(p1, p2)

    a = vector_dot(d1, d1)
    e = vector_dot(d2, d2)
    f = vector_dot(d2, r)

    if a <= EPSILON and e <= EPSILON:
        return vector_length(r)

    if a <= EPSILON:
        s = 0.0
        t = clamp(f / e, 0.0, 1.0)
    else:
        c = vector_dot(d1, r)
        if e <= EPSILON:
            t = 0.0
            s = clamp(-c / a, 0.0, 1.0)
        else:
            b = vector_dot(d1, d2)
            denominator = a * e - b * b
            if abs(denominator) > EPSILON:
                s = clamp((b * f - c * e) / denominator, 0.0, 1.0)
            else:
                s = 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t = 0.0
                s = clamp(-c / a, 0.0, 1.0)
            elif t > 1.0:
                t = 1.0
                s = clamp((b - c) / a, 0.0, 1.0)

    closest_a = vector_add(p1, vector_scale(d1, s))
    closest_b = vector_add(p2, vector_scale(d2, t))
    return vector_length(vector_sub(closest_a, closest_b))


# ---------------------------------------------------------------
# 5. BUNDLE PACKING AND COLLISION AVOIDANCE
# ---------------------------------------------------------------
# Place n strands on a Fermat (sunflower) spiral, normalised to a
# unit radius. The golden angle spaces points as evenly as possible
# for a given radius, which is exactly what a cable bundle wants.
def sunflower_offsets(count):
    count = max(1, int(count))
    if count == 1:
        return [(0.0, 0.0)]

    raw = []
    for k in range(count):
        radius = math.sqrt((k + 0.5) / float(count))
        angle = k * GOLDEN_ANGLE
        raw.append((radius * math.cos(angle), radius * math.sin(angle)))

    longest = max(math.sqrt(x * x + y * y) for x, y in raw)
    if longest < EPSILON:
        return raw
    return [(x / longest, y / longest) for x, y in raw]


# Tightest gap in the 2D pattern - the number the spread has to be
# scaled against to guarantee clearance.
def min_pattern_distance(offsets):
    if len(offsets) < 2:
        return float("inf")

    best = float("inf")
    for i in range(len(offsets)):
        for j in range(i + 1, len(offsets)):
            dx = offsets[i][0] - offsets[j][0]
            dy = offsets[i][1] - offsets[j][1]
            best = min(best, math.sqrt(dx * dx + dy * dy))
    return best


# Two tubes touch when their centres are 2 radii apart, so this is
# that plus a small margin.
def required_clearance(radius, margin=SAFETY_MARGIN):
    return 2.0 * max(0.0, radius) + max(0.0, margin)


# Scale the pattern so its tightest gap meets the clearance. If the
# user's Bundle Spread is already big enough it is returned as-is;
# otherwise the minimum is returned along with a flag, and that flag
# is the ONLY thing that triggers the radius-too-large warning.
def resolve_bundle_spread(count, spread, radius, margin=SAFETY_MARGIN):
    count = max(1, int(count))
    if count == 1:
        return max(0.0, spread), False

    offsets = sunflower_offsets(count)
    unit_gap = min_pattern_distance(offsets)
    if unit_gap < EPSILON or unit_gap == float("inf"):
        return max(0.0, spread), False

    needed = required_clearance(radius, margin) / unit_gap
    if needed > spread + EPSILON:
        return needed, True
    return spread, False


# Length Stagger: pick the slice of the span this strand occupies.
# At slider 0 this returns exactly (0.0, 1.0) so every strand runs
# anchor to anchor; higher values trim each end at random.
def stagger_range(stagger):
    amount = clamp(stagger, 0.0, STAGGER_SAFE_MAX) / STAGGER_SAFE_MAX
    if amount <= 0.0:
        return 0.0, 1.0

    head = random.uniform(0.0, STAGGER_MAX_TRIM * amount)
    tail = random.uniform(0.0, STAGGER_MAX_TRIM * amount)
    start = head
    end = 1.0 - tail

    if end - start < STAGGER_MIN_SPAN:
        centre = (start + end) * 0.5
        start = max(0.0, centre - STAGGER_MIN_SPAN * 0.5)
        end = min(1.0, start + STAGGER_MIN_SPAN)

    return start, end


# The normalised catenary - the shape a real hanging chain makes.
# Returns exactly 0 at both anchors and -1 at the midpoint, so the
# Gravity Sag slider reads directly as a fraction of span length.
# Degenerate tension falls back to a parabola rather than dividing
# by zero.
def catenary_offset(t, tension=CATENARY_TENSION):
    if tension < EPSILON:
        return -4.0 * t * (1.0 - t)

    try:
        numerator = math.cosh(tension * (2.0 * t - 1.0)) - math.cosh(tension)
        denominator = math.cosh(tension) - 1.0
    except OverflowError:
        raise CableRigError(
            "Catenary tension overflowed while sampling the cable curve.")

    if abs(denominator) < EPSILON:
        return -4.0 * t * (1.0 - t)

    return numerator / denominator


# Sample the curve, returning both the points and their parameter
# values. The parameters matter: a trimmed strand must still know
# where it sits on the FULL span so its frame and twist line up
# with the other strands.
def sample_span_params(start, end, sag_factor, resolution,
                       tension=CATENARY_TENSION, t_start=0.0, t_end=1.0):
    resolution = max(1, int(resolution))
    points = []
    parameters = []

    for i in range(resolution + 1):
        local = float(i) / float(resolution)
        t = t_start + (t_end - t_start) * local
        parameters.append(t)
        points.append(span_point(start, end, sag_factor, t, tension))

    return points, parameters


# Convenience wrapper for callers that only want the points.
def sample_span(start, end, sag_factor, resolution,
                tension=CATENARY_TENSION, t_start=0.0, t_end=1.0):
    points, _parameters = sample_span_params(start, end, sag_factor,
                                             resolution, tension,
                                             t_start, t_end)
    return points


# Direction of travel at a sample, using a central difference in
# the middle and a one-sided difference at the ends.
def path_tangent(points, index):
    last = len(points) - 1
    if last <= 0:
        return (1.0, 0.0, 0.0)
    if index <= 0:
        return vector_sub(points[1], points[0])
    if index >= last:
        return vector_sub(points[last], points[last - 1])
    return vector_sub(points[index + 1], points[index - 1])


# Push one strand off the centre line by its pattern offset.
#
# The twist angle is driven by the GLOBAL parameter t, not the
# sample index. That distinction matters: with Length Stagger on,
# index 5 of a trimmed strand is somewhere different along the span
# than index 5 of a full strand, and using the index made them
# rotate into each other.
def offset_strand(base_points, parameters, offset_xy, spread,
                  twist_degrees, field):
    if spread <= EPSILON or (abs(offset_xy[0]) < EPSILON
                             and abs(offset_xy[1]) < EPSILON):
        return list(base_points)

    twist_radians = math.radians(twist_degrees)
    result = []

    for point, t in zip(base_points, parameters):
        side, up = frame_at(field, t)
        angle = twist_radians * t
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        rx = offset_xy[0] * cos_a - offset_xy[1] * sin_a
        ry = offset_xy[0] * sin_a + offset_xy[1] * cos_a
        offset = vector_add(vector_scale(side, rx * spread),
                            vector_scale(up, ry * spread))
        result.append(vector_add(point, offset))

    return result


# Reference implementations used to validate the fast path below.
# Correct but slow - kept because a fast algorithm you have not
# checked against a simple one is just a guess.
def brute_force_min_gap(strands):
    best = float("inf")
    for a in range(len(strands)):
        for b in range(a + 1, len(strands)):
            for p in strands[a]:
                for q in strands[b]:
                    distance = vector_length(vector_sub(p, q))
                    if distance < best:
                        best = distance
    return best


def brute_force_segment_gap(strands):
    best = float("inf")
    for a in range(len(strands)):
        for b in range(a + 1, len(strands)):
            for i in range(len(strands[a]) - 1):
                for j in range(len(strands[b]) - 1):
                    distance = segment_segment_distance(
                        strands[a][i], strands[a][i + 1],
                        strands[b][j], strands[b][j + 1])
                    if distance < best:
                        best = distance
    return best


# Fast tightest-gap search using a spatial hash.
#
# Cells are sized so that any pair closer than the clearance must
# land in neighbouring cells, which makes the result exact for the
# only question being asked: is anything too close?
def bundle_min_gap(strands, clearance):
    if len(strands) < 2:
        return float("inf")

    segments = []
    longest = 0.0
    for index, points in enumerate(strands):
        for i in range(len(points) - 1):
            head = points[i]
            tail = points[i + 1]
            middle = vector_scale(vector_add(head, tail), 0.5)
            half = vector_length(vector_sub(tail, head)) * 0.5
            longest = max(longest, half)
            segments.append((index, head, tail, middle))

    if not segments:
        return float("inf")

    cell = max(float(clearance) + 2.0 * longest, EPSILON)
    grid = {}
    for entry in segments:
        middle = entry[3]
        key = (int(math.floor(middle[0] / cell)),
               int(math.floor(middle[1] / cell)),
               int(math.floor(middle[2] / cell)))
        grid.setdefault(key, []).append(entry)

    best = float("inf")
    for key, bucket in grid.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    neighbour = grid.get((key[0] + dx, key[1] + dy,
                                          key[2] + dz))
                    if not neighbour:
                        continue
                    for index_a, head_a, tail_a, _mid_a in bucket:
                        for index_b, head_b, tail_b, _mid_b in neighbour:
                            if index_a >= index_b:
                                continue
                            distance = segment_segment_distance(
                                head_a, tail_a, head_b, tail_b)
                            if distance < best:
                                best = distance

    return best


# Build every strand of one span at the current spread and slack
# scale. Called repeatedly by the relaxation loop below, which is
# why it takes those two as parameters rather than reading them
# from settings.
def build_bundle(start, end, settings, offsets, spread, field=None,
                 slack_scale=1.0):
    if field is None:
        field = span_frame_field(start, end, settings["sag"],
                                 settings["resolution"])

    strands = []
    for offset in offsets:
        t_start, t_end = stagger_range(settings["stagger"])
        raw = slack_multiplier(settings.get("slack", SLACK_SAFE_MIN))
        strand_sag = settings["sag"] * (1.0 + (raw - 1.0)
                                        * max(0.0, slack_scale))
        base, parameters = sample_span_params(start, end, strand_sag,
                                              settings["resolution"],
                                              t_start=t_start,
                                              t_end=t_end)
        strands.append(offset_strand(base, parameters, offset, spread,
                                     settings["twist"], field))
    return strands


# The collision-avoidance loop, and the heart of the tool.
#
# Build the bundle, measure the true segment-to-segment gap, and if
# anything is too close adjust and try again. There are two levers:
#   - widen the bundle, which fixes ordinary crowding;
#   - ease the slack variation, which is the only thing that helps
#     when one strand sags THROUGH another (no amount of widening
#     separates those).
# The random state is restored before each attempt so the stagger
# and slack draws stay identical between passes; otherwise we would
# be measuring a different bundle every time.
# If it still cannot separate them it returns resolved=False rather
# than pretending success - the UI then says so plainly.
def relax_bundle(start, end, settings, seed_state=None):
    offsets = sunflower_offsets(settings["bundle"])
    clearance = required_clearance(settings["radius"])
    spread, enforced = resolve_bundle_spread(settings["bundle"],
                                             settings["spread"],
                                             settings["radius"])

    requested_spread = settings["spread"]
    field = span_frame_field(start, end, settings["sag"],
                             settings["resolution"])

    if settings["bundle"] <= 1:
        if seed_state is not None:
            random.setstate(seed_state)
        return {"strands": build_bundle(start, end, settings, offsets,
                                        spread, field),
                "spread": spread, "enforced": False, "passes": 0,
                "resolved": True, "gap": float("inf"),
                "clearance": clearance, "slack_scale": 1.0,
                "pattern_enforced": enforced,
                "spread_grew": False, "slack_eased": False}

    pattern_enforced = enforced
    ceiling = max(spread, clearance) * RELAX_GROWTH_CAP
    passes = 0
    strands = []
    gap = 0.0
    slack_scale = 1.0
    slack_eased = False

    for attempt in range(RELAX_MAX_PASSES):
        if seed_state is not None:
            random.setstate(seed_state)
        strands = build_bundle(start, end, settings, offsets, spread, field,
                               slack_scale)
        gap = bundle_min_gap(strands, clearance)
        passes += 1

        if gap >= clearance - EPSILON or gap == float("inf"):
            return {"strands": strands, "spread": spread,
                    "enforced": enforced, "passes": passes,
                    "resolved": True, "gap": gap, "clearance": clearance,
                    "slack_scale": slack_scale,
                    "pattern_enforced": pattern_enforced,
                    "spread_grew": spread > requested_spread + EPSILON,
                    "slack_eased": slack_eased}

        growth = min(max((clearance / max(gap, EPSILON)) * RELAX_EASING,
                         RELAX_MIN_GROWTH), RELAX_MAX_GROWTH)
        new_spread = min(ceiling, spread * growth)
        widened = new_spread > spread + EPSILON
        if widened:
            spread = new_spread
            enforced = True

        # Widening the bundle cannot separate strands that sag THROUGH
        # each other, so once growth stalls the slack variation is the
        # lever that has to give.
        damped = False
        if slack_scale > 0.0 and (not widened
                                  or attempt >= SLACK_DAMP_AFTER):
            slack_scale *= SLACK_DAMP
            if slack_scale < 0.05:
                slack_scale = 0.0
            damped = True
            slack_eased = True
            enforced = True

        if not widened and not damped:
            break

    return {"strands": strands, "spread": spread, "enforced": True,
            "passes": passes, "resolved": False, "gap": gap,
            "clearance": clearance, "slack_scale": slack_scale,
            "pattern_enforced": pattern_enforced,
            "spread_grew": spread > requested_spread + EPSILON,
            "slack_eased": slack_eased}


# ---------------------------------------------------------------
# 6. READING THE SCENE
# ---------------------------------------------------------------
# Read world positions from the selection. Normal mode filters to
# transforms; beta mode deliberately does not, so that selecting a
# shader can be demonstrated failing gracefully. Anything without a
# position is reported rather than silently dropped.
def collect_anchor_positions(beta=False):
    if not MAYA_AVAILABLE:
        return [], []

    if beta:
        selection = cmds.ls(selection=True, long=True) or []
    else:
        selection = cmds.ls(selection=True, long=True,
                            type="transform") or []

    positions = []
    rejected = []

    for node in selection:
        try:
            value = cmds.xform(node, query=True, worldSpace=True,
                               translation=True)
        except Exception:
            rejected.append(node)
            continue

        if not value or len(value) < 3:
            rejected.append(node)
            continue

        positions.append((float(value[0]), float(value[1]),
                          float(value[2])))

    return positions, rejected


# Count anchor pairs sitting on top of each other. Catching these
# early is what keeps a zero-length span from dividing by zero.
def degenerate_span_count(positions):
    count = 0
    for i in range(len(positions) - 1):
        if vector_length(vector_sub(positions[i + 1],
                                    positions[i])) < EPSILON:
            count += 1
    return count


# ---------------------------------------------------------------
# 7. SHADING
# ---------------------------------------------------------------
# Clamp a colour from the picker into valid 0-1 RGB.
def sanitize_color(rgb):
    if not rgb or len(rgb) < 3:
        return (0.15, 0.15, 0.17)
    return (clamp(rgb[0], 0.0, 1.0), clamp(rgb[1], 0.0, 1.0),
            clamp(rgb[2], 0.0, 1.0))


# Build a Lambert plus its shading group and wire them together.
def create_shader(name, rgb):
    shader = cmds.shadingNode("lambert", asShader=True, name=name)
    cmds.setAttr(shader + ".color", rgb[0], rgb[1], rgb[2], type="double3")
    shading_group = cmds.sets(renderable=True, noSurfaceShader=True,
                              empty=True, name=shader + "SG")
    cmds.connectAttr(shader + ".outColor",
                     shading_group + ".surfaceShader", force=True)
    return shader, shading_group


# Fetch the tool's single shared shader, creating it only if it is
# missing. Updating the existing one in place is what stops every
# Generate press from littering the Hypershade with duplicates.
def cable_material(rgb):
    rgb = sanitize_color(rgb)
    if cmds.objExists(CUSTOM_MATERIAL):
        cmds.setAttr(CUSTOM_MATERIAL + ".color", rgb[0], rgb[1], rgb[2],
                     type="double3")
        group = CUSTOM_MATERIAL + "SG"
        if cmds.objExists(group):
            return CUSTOM_MATERIAL, group
        cmds.delete(CUSTOM_MATERIAL)

    return create_shader(CUSTOM_MATERIAL, rgb)


# Assign a shading group to a mesh.
def assign_material(obj, shading_group):
    cmds.sets(obj, edit=True, forceElement=shading_group)


# ---------------------------------------------------------------
# 8. BUILDING THE GEOMETRY
# ---------------------------------------------------------------
# The sampled points become a NURBS curve. These are kept in the
# scene rather than deleted, because they are useful downstream for
# rigging or re-extruding at a different thickness.
def create_nurbs_curve(points, name):
    return cmds.curve(degree=3, point=[list(p) for p in points], name=name)


# Primary geometry path: sweep a circle along the curve to make a
# NURBS tube, then convert it to polygons. The temporary profile
# and surface are cleaned up in a finally block so a failure part
# way through cannot leave junk in the scene.
def extrude_tube(curve, radius, resolution, name):
    profile = cmds.circle(normal=(0.0, 1.0, 0.0), radius=radius,
                          sections=PROFILE_SECTIONS,
                          constructionHistory=False)[0]
    surface = None
    try:
        surface = cmds.extrude(profile, curve, extrudeType=2,
                               useComponentPivot=1, fixedPath=True,
                               useProfileNormal=True,
                               reverseSurfaceIfPathReversed=True,
                               constructionHistory=False)[0]
        mesh = cmds.nurbsToPoly(surface, constructionHistory=False,
                                format=2, polygonType=1,
                                uType=1, uNumber=PROFILE_SECTIONS,
                                vType=1, vNumber=max(2, int(resolution)),
                                matchNormalDir=True, name=name)[0]
    finally:
        for temp in (surface, profile):
            if temp and cmds.objExists(temp):
                cmds.delete(temp)

    return mesh


# Fallback geometry path: a chain of cylinders aimed along each
# segment, welded with polyUnite. Less elegant, but it depends only
# on polyCylinder, so it still works if the extrude flags above are
# unavailable in a given Maya build.
def segment_tube(points, radius, name):
    pieces = []
    for i in range(len(points) - 1):
        direction = vector_sub(points[i + 1], points[i])
        length = vector_length(direction)
        if length < EPSILON:
            continue

        piece = cmds.polyCylinder(radius=radius, height=length,
                                  axis=direction,
                                  subdivisionsX=PROFILE_SECTIONS,
                                  subdivisionsY=1, subdivisionsZ=1,
                                  constructionHistory=False,
                                  name=name + "_seg")[0]
        midpoint = vector_scale(vector_add(points[i], points[i + 1]), 0.5)
        cmds.move(midpoint[0], midpoint[1], midpoint[2], piece,
                  absolute=True)
        pieces.append(piece)

    if not pieces:
        return None
    if len(pieces) == 1:
        return cmds.rename(pieces[0], name)

    return cmds.polyUnite(pieces, constructionHistory=False, name=name)[0]


# Try the extrude path, drop to the segment path if it fails, and
# report which one ran. In beta mode the fallback is switched off
# on purpose so failures surface instead of being papered over.
def build_cable_mesh(points, radius, resolution, name, allow_fallback=True):
    curve = create_nurbs_curve(points, name.replace(GEO_PREFIX,
                                                    CURVE_PREFIX))
    try:
        mesh = extrude_tube(curve, radius, resolution, name)
        return mesh, curve, "extrude"
    except Exception as err:
        if not allow_fallback:
            raise
        print("[CableHoseRigTool] extrude path failed ({0}); using "
              "segment fallback.".format(err))
        mesh = segment_tube(points, radius, name)
        return mesh, curve, "segments"


# ---------------------------------------------------------------
# 9. ORCHESTRATION
# ---------------------------------------------------------------
# The main entry point: read the selection, sanitise the settings,
# build every span, group the result and compose a status message.
#
# MANDATORY HABIT 1 - the whole build runs inside a single undo
# chunk in a try/finally, so a bundle of dozens of strands undoes
# with one Ctrl+Z and the chunk closes even if something raises.
#
# The message is assembled from three distinct kinds of event, kept
# separate on purpose: routine auto-fitting (reported as OK), a
# genuine radius-versus-spread conflict (the required warning), and
# an unresolved collision (an error). Conflating them was a real
# bug - the warning then fired on every single run.
def generate_cable_rig(sag, radius, resolution, bundle, spread, twist,
                       stagger=0.0, slack=SLACK_SAFE_MIN,
                       color=(0.15, 0.15, 0.17), beta=False, seed=None):
    if seed is not None:
        random.seed(seed)

    positions, rejected = collect_anchor_positions(beta=beta)

    if len(positions) < 2:
        message = "Please select 2 or more objects/locators."
        if rejected:
            message += (" {0} selected item(s) had no world position "
                        "and were ignored.".format(len(rejected)))
        return {"status": "warning", "message": message, "cables": 0,
                "strands": 0, "skipped": 0, "collision_enforced": False}

    settings = sanitize_settings(sag, radius, resolution, bundle, spread,
                                 twist, stagger, slack, beta=beta)
    span_count = len(positions) - 1
    degenerate = degenerate_span_count(positions)

    if degenerate == span_count:
        return {"status": "warning",
                "message": "All selected anchors share the same position; "
                           "there is nothing to span.",
                "cables": 0, "strands": 0, "skipped": degenerate,
                "collision_enforced": False}

    if beta:
        audit_extreme_settings(settings, span_count)

    meshes = []
    curves = []
    skipped = 0
    method = "extrude"
    enforced_any = False
    final_spread = settings["spread"]
    relax_passes = 0
    unresolved = []
    slack_scale_min = 1.0
    pattern_enforced = False
    spread_grew = False
    slack_eased = False

    cmds.undoInfo(openChunk=True, chunkName="GenerateCableRig")
    try:
        shader, shading_group = cable_material(color)

        for span_index in range(span_count):
            start = positions[span_index]
            end = positions[span_index + 1]

            if vector_length(vector_sub(end, start)) < EPSILON:
                skipped += 1
                continue

            relaxed = relax_bundle(start, end, settings,
                                   seed_state=random.getstate())
            enforced_any = enforced_any or relaxed["enforced"]
            pattern_enforced = pattern_enforced or relaxed["pattern_enforced"]
            spread_grew = spread_grew or relaxed["spread_grew"]
            slack_eased = slack_eased or relaxed["slack_eased"]
            final_spread = max(final_spread, relaxed["spread"])
            relax_passes = max(relax_passes, relaxed["passes"])
            if not relaxed["resolved"]:
                unresolved.append((span_index, relaxed["gap"],
                                   relaxed["clearance"]))
            slack_scale_min = min(slack_scale_min,
                                  relaxed.get("slack_scale", 1.0))

            for strand_index, points in enumerate(relaxed["strands"]):
                name = "{0}s{1:02d}_c{2:02d}".format(GEO_PREFIX, span_index,
                                                     strand_index)
                mesh, curve, used = build_cable_mesh(
                    points, settings["radius"], settings["resolution"],
                    name, allow_fallback=not beta)

                if used != "extrude":
                    method = used
                if curve:
                    curves.append(curve)
                if mesh:
                    assign_material(mesh, shading_group)
                    meshes.append(mesh)

        group_node = assemble_rig(meshes, curves)
    finally:
        cmds.undoInfo(closeChunk=True)

    parts = ["Built {0} cable(s) across {1} span(s) using the {2} "
             "path.".format(len(meshes), span_count - skipped, method)]
    if skipped:
        parts.append("Skipped {0} zero-length span(s).".format(skipped))
    if rejected:
        parts.append("Ignored {0} non-transform item(s).".format(
            len(rejected)))
    notes = []
    if settings["resolution"] > settings.get("resolution_requested",
                                             settings["resolution"]):
        notes.append("resolution {0}".format(settings["resolution"]))
    if spread_grew:
        notes.append("spread {0:.3f}".format(final_spread))
    if slack_eased and settings["slack"] > SLACK_SAFE_MIN:
        notes.append("slack {0:.0f}%".format(slack_scale_min * 100.0))

    if pattern_enforced:
        minimum = resolve_bundle_spread(settings["bundle"], 0.0,
                                        settings["radius"])[0]
        parts.append(COLLISION_WARNING)
        parts.append("Set Bundle Spread to {0:.3f} or more (for {1} strands "
                     "at radius {2:.3f}) to avoid this.".format(
                         minimum, settings["bundle"], settings["radius"]))
    elif notes:
        parts.append("Auto-fit: " + ", ".join(notes) + ".")

    if unresolved:
        worst = min(u[1] for u in unresolved)
        need = unresolved[0][2]
        parts.append("NOT FULLY RESOLVED: {0} span(s) still have cables "
                     "within {1:.4f} of each other (need {2:.4f}). Reduce "
                     "Bundle Count or Cable Radius.".format(
                         len(unresolved), worst, need))

    if unresolved:
        level = "error"
    elif pattern_enforced:
        level = "warning"
    else:
        level = "ok"

    return {"status": level,
            "message": " ".join(parts), "group": group_node,
            "cables": len(meshes), "strands": settings["bundle"],
            "skipped": skipped, "collision_enforced": enforced_any,
            "spread": final_spread, "passes": relax_passes,
            "unresolved": len(unresolved),
            "slack_scale": slack_scale_min,
            "pattern_enforced": pattern_enforced,
            "auto_fit": bool(notes)}


# Parent the output into the CableRig_GRP hierarchy so it is easy
# to select, hide or delete as one unit.
def assemble_rig(meshes, curves):
    sub_groups = []

    for members, name in ((meshes, GEO_GROUP), (curves, CURVE_GROUP)):
        if not members:
            continue
        cmds.select(members, replace=True)
        sub_groups.append(cmds.group(members, name=name))

    if not sub_groups:
        return None

    cmds.select(sub_groups, replace=True)
    group_node = cmds.group(sub_groups, name=GROUP_NAME)
    cmds.select(group_node, replace=True)
    return group_node


# MANDATORY HABIT 2 - safe deletion.
#
# Remove the groups, then sweep for strays BY EXPLICIT PREFIX only.
# There is deliberately no bare wildcard anywhere here: a stray
# cmds.ls("*") would happily delete the artist's own work. Also
# wrapped in its own undo chunk so a clear can be undone in one go.
def clear_scene(*_args):
    deleted = 0

    cmds.undoInfo(openChunk=True, chunkName="ClearCableRig")
    try:
        for group in (GROUP_NAME, GEO_GROUP, CURVE_GROUP):
            if cmds.objExists(group):
                cmds.delete(group)
                deleted += 1

        for prefix in (GEO_PREFIX, CURVE_PREFIX):
            strays = cmds.ls(prefix + "*", type="transform") or []
            if strays:
                cmds.delete(strays)
                deleted += len(strays)

        shaders = cmds.ls(MATERIAL_PREFIX + "*", materials=True) or []
        sgs = cmds.ls(MATERIAL_PREFIX + "*SG") or []
        junk = [n for n in shaders + sgs if cmds.objExists(n)]
        if junk:
            cmds.delete(junk)
            deleted += len(junk)
    finally:
        cmds.undoInfo(closeChunk=True)

    set_status("Cleared {0} node(s).".format(deleted))
    return deleted


# ---------------------------------------------------------------
# 10. USER INTERFACE
# ---------------------------------------------------------------
# Write to the status box and the Script Editor at once. The box is
# a fixed-height scrollField rather than a text label, because a
# label grows with its content and was resizing the whole window
# whenever a long message appeared.
def set_status(message, level="info"):
    tags = {"info": "[INFO]", "ok": "[OK]", "warning": "[WARNING]",
            "error": "[ERROR]"}
    text = "{0} {1}".format(tags.get(level, tags["info"]), message)
    print("[CableHoseRigTool] " + text)

    if not MAYA_AVAILABLE:
        return
    if not cmds.scrollField(CTRL_STATUS, exists=True):
        return

    cmds.scrollField(CTRL_STATUS, edit=True, text=text, insertionPosition=1)
    colours = {"info": (0.22, 0.22, 0.22),
               "ok": (0.16, 0.30, 0.19),
               "warning": (0.36, 0.29, 0.11),
               "error": (0.36, 0.17, 0.16)}
    try:
        cmds.scrollField(CTRL_STATUS, edit=True,
                         backgroundColor=colours.get(level, colours["info"]))
    except Exception:
        pass


# Generate button. Reads every control, then wraps the call in two
# handlers: CableRigError for limits we chose to enforce, and a
# catch-all for anything unforeseen. Either way the user gets a
# message instead of a crash.
def _on_generate(*_args):
    beta = cmds.checkBox(CTRL_BETA, query=True, value=True)

    try:
        result = generate_cable_rig(
            sag=cmds.floatSliderGrp(CTRL_SAG, query=True, value=True),
            radius=cmds.floatSliderGrp(CTRL_RADIUS, query=True, value=True),
            resolution=cmds.intSliderGrp(CTRL_RESOLUTION, query=True,
                                         value=True),
            bundle=cmds.intSliderGrp(CTRL_BUNDLE, query=True, value=True),
            spread=cmds.floatSliderGrp(CTRL_SPREAD, query=True, value=True),
            twist=cmds.floatSliderGrp(CTRL_TWIST, query=True, value=True),
            stagger=cmds.floatSliderGrp(CTRL_STAGGER, query=True,
                                        value=True),
            slack=cmds.floatSliderGrp(CTRL_SLACK, query=True, value=True),
            color=cmds.colorSliderGrp(CTRL_COLOR, query=True,
                                      rgbValue=True),
            beta=beta,
        )
    except CableRigError as err:
        set_status("BETA LIMIT: {0}".format(err), "warning")
        return
    except Exception as err:
        set_status("UNEXPECTED ERROR HANDLED: {0}: {1}".format(
            type(err).__name__, err), "error")
        return

    set_status(result["message"], result["status"])


# Help menu item: tells the user the smallest Bundle Spread that
# will fit the current strand count and radius, so the collision
# warning becomes something they can act on rather than just read.
def _on_show_minimum(*_args):
    bundle = cmds.intSliderGrp(CTRL_BUNDLE, query=True, value=True)
    radius = cmds.floatSliderGrp(CTRL_RADIUS, query=True, value=True)
    spread = cmds.floatSliderGrp(CTRL_SPREAD, query=True, value=True)
    minimum = resolve_bundle_spread(bundle, 0.0, radius)[0]

    if spread + EPSILON >= minimum:
        set_status(
            "Bundle Spread {0:.3f} is above the minimum {1:.3f} needed for "
            "{2} strands at radius {3:.3f}.".format(spread, minimum,
                                                    int(bundle), radius),
            "ok")
    else:
        set_status(
            "Bundle Spread {0:.3f} is below the minimum {1:.3f} needed for "
            "{2} strands at radius {3:.3f}; it will be raised "
            "automatically.".format(spread, minimum, int(bundle), radius),
            "warning")


# Warn as soon as the strand count outgrows the current spread,
# rather than waiting until the user presses Generate.
def _on_bundle_changed(*_args):
    try:
        bundle = cmds.intSliderGrp(CTRL_BUNDLE, query=True, value=True)
        radius = cmds.floatSliderGrp(CTRL_RADIUS, query=True, value=True)
        spread = cmds.floatSliderGrp(CTRL_SPREAD, query=True, value=True)
    except Exception:
        return

    minimum = resolve_bundle_spread(bundle, 0.0, radius)[0]
    if spread + EPSILON < minimum:
        set_status(
            "{0} strands at radius {1:.3f} need a Bundle Spread of at least "
            "{2:.3f}; currently {3:.3f}.".format(int(bundle), radius,
                                                 minimum, spread),
            "info")


# Clear Rig button.
def _on_clear(*_args):
    clear_scene()


# Restore every control to its default value.
def _on_reset(*_args):
    cmds.floatSliderGrp(CTRL_SAG, edit=True, value=0.25)
    cmds.floatSliderGrp(CTRL_RADIUS, edit=True, value=0.08)
    cmds.intSliderGrp(CTRL_RESOLUTION, edit=True, value=16)
    cmds.intSliderGrp(CTRL_BUNDLE, edit=True, value=5)
    cmds.floatSliderGrp(CTRL_SPREAD, edit=True, value=0.6)
    cmds.floatSliderGrp(CTRL_TWIST, edit=True, value=180.0)
    cmds.floatSliderGrp(CTRL_STAGGER, edit=True, value=0.0)
    cmds.floatSliderGrp(CTRL_SLACK, edit=True, value=SLACK_SAFE_MIN)
    cmds.colorSliderGrp(CTRL_COLOR, edit=True, rgbValue=(0.15, 0.15, 0.17))
    cmds.checkBox(CTRL_BETA, edit=True, value=False)
    set_status("Settings reset to defaults.")


# One shared column layout so every label and field lines up.
def _slider_columns():
    return (150, 62, 230)


# Build the tool window.
#
# Layout choices worth knowing: a formLayout root holds a scrolling
# body above a PINNED footer, so the buttons and status bar never
# scroll out of reach; the sections are collapsible frameLayouts;
# and the Beta checkbox lives under Advanced, collapsed, because it
# is a testing switch rather than an everyday control.
def show_ui(*_args):
    if not MAYA_AVAILABLE:
        raise RuntimeError(
            "show_ui() requires Maya. Run this file from a terminal to "
            "push it to a running Maya session over the command port."
        )

    if cmds.window(WINDOW_NAME, exists=True):
        cmds.deleteUI(WINDOW_NAME, window=True)

    window = cmds.window(WINDOW_NAME, title=WINDOW_TITLE,
                         widthHeight=(520, 640), sizeable=True,
                         menuBarVisible=True)

    cmds.menuBarLayout()
    cmds.menu(label="Edit")
    cmds.menuItem(label="Reset Settings", command=_on_reset)
    cmds.menuItem(label="Clear Rig", command=_on_clear)
    cmds.menu(label="Help", helpMenu=True)
    cmds.menuItem(label="Minimum Spread For Current Settings",
                  command=_on_show_minimum)

    form = cmds.formLayout(numberOfDivisions=100)

    body = cmds.scrollLayout(childResizable=True,
                             horizontalScrollBarThickness=0, parent=form)

    cmds.frameLayout(label="Cable Shape", collapsable=True, collapse=False,
                     marginWidth=8, marginHeight=6, parent=body)
    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)
    cmds.floatSliderGrp(
        CTRL_SAG, label="Gravity Sag", field=True,
        minValue=0.0, maxValue=1.5, fieldMinValue=0.0,
        fieldMaxValue=SAG_SAFE_MAX, value=0.25, precision=3,
        columnWidth3=_slider_columns(),
        annotation="Catenary sag depth as a fraction of the span length. "
                   "0 = taut straight cable.")
    cmds.floatSliderGrp(
        CTRL_RADIUS, label="Cable Radius", field=True,
        minValue=0.01, maxValue=2.0, fieldMinValue=RADIUS_SAFE_MIN,
        fieldMaxValue=RADIUS_SAFE_MAX, value=0.08, precision=3,
        columnWidth3=_slider_columns(),
        annotation="Tube thickness. Also sets the minimum clearance "
                   "between strands.")
    cmds.intSliderGrp(
        CTRL_RESOLUTION, label="Curve Resolution", field=True,
        minValue=RESOLUTION_SAFE_MIN, maxValue=RESOLUTION_SAFE_MAX,
        fieldMinValue=RESOLUTION_SAFE_MIN,
        fieldMaxValue=RESOLUTION_SAFE_MAX, value=16,
        columnWidth3=_slider_columns(),
        annotation="Samples per span. Raised automatically when Spiral "
                   "Twist needs finer sampling.")
    cmds.setParent(body)

    cmds.frameLayout(label="Bundling", collapsable=True, collapse=False,
                     marginWidth=8, marginHeight=6, parent=body)
    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)
    cmds.intSliderGrp(
        CTRL_BUNDLE, label="Bundle Count", field=True,
        minValue=BUNDLE_SAFE_MIN, maxValue=BUNDLE_SAFE_MAX,
        fieldMinValue=BUNDLE_SAFE_MIN, fieldMaxValue=BUNDLE_SAFE_MAX,
        value=5, columnWidth3=_slider_columns(),
        changeCommand=_on_bundle_changed,
        annotation="Strands per span, arranged on a Fermat (sunflower) "
                   "spiral.")
    cmds.floatSliderGrp(
        CTRL_SPREAD, label="Bundle Spread", field=True,
        minValue=0.0, maxValue=3.0, fieldMinValue=0.0,
        fieldMaxValue=SPREAD_SAFE_MAX, value=0.6, precision=3,
        columnWidth3=_slider_columns(),
        annotation="Scatter radius of the strands. Must be at least the "
                   "minimum shown under Help, or it is raised for you.")
    cmds.floatSliderGrp(
        CTRL_TWIST, label="Spiral Twist", field=True,
        minValue=-720.0, maxValue=720.0, fieldMinValue=-TWIST_SAFE_MAX,
        fieldMaxValue=TWIST_SAFE_MAX, value=180.0, precision=1,
        columnWidth3=_slider_columns(),
        annotation="Degrees the bundle rotates along a span. The pattern "
                   "turns rigidly, so clearance is preserved.")
    cmds.floatSliderGrp(
        CTRL_STAGGER, label="Length Stagger", field=True,
        minValue=0.0, maxValue=STAGGER_SAFE_MAX, fieldMinValue=0.0,
        fieldMaxValue=STAGGER_SAFE_MAX, value=0.0, precision=2,
        columnWidth3=_slider_columns(),
        annotation="0 = every strand runs anchor to anchor. 10 = strands "
                   "are trimmed short for a broken, layered bundle.")
    cmds.floatSliderGrp(
        CTRL_SLACK, label="Slack Variation", field=True,
        minValue=SLACK_SAFE_MIN, maxValue=SLACK_SAFE_MAX,
        fieldMinValue=SLACK_SAFE_MIN, fieldMaxValue=SLACK_SAFE_MAX,
        value=SLACK_SAFE_MIN, precision=2,
        columnWidth3=_slider_columns(),
        annotation="1 = identical sag on every strand. 10 = each strand "
                   "hangs with its own slack. Strands still meet both "
                   "anchors.")
    cmds.setParent(body)

    cmds.frameLayout(label="Shading", collapsable=True, collapse=False,
                     marginWidth=8, marginHeight=6, parent=body)
    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)
    cmds.colorSliderGrp(
        CTRL_COLOR, label="Cable Color", rgbValue=(0.15, 0.15, 0.17),
        columnWidth3=_slider_columns(),
        annotation="Exact Lambert colour applied to every strand through "
                   "the shared " + CUSTOM_MATERIAL + " material.")
    cmds.setParent(body)

    cmds.frameLayout(label="Advanced", collapsable=True, collapse=True,
                     marginWidth=8, marginHeight=6, parent=body)
    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)
    cmds.checkBox(
        CTRL_BETA,
        label="Experimental Extreme Sag Physics (Beta)",
        value=False,
        annotation="Unlocks the safety clamps so extreme values and "
                   "non-transform selections can be tested. Failures are "
                   "reported in the status bar instead of crashing.")
    cmds.setParent(body)
    cmds.setParent(form)

    footer = cmds.columnLayout(adjustableColumn=True, rowSpacing=4,
                               columnOffset=("both", 8), parent=form)
    cmds.separator(height=6, style="none")

    buttons = cmds.formLayout(numberOfDivisions=100)
    generate = cmds.button(label="Generate Cable Rig", height=38,
                           backgroundColor=(0.30, 0.52, 0.36),
                           command=_on_generate,
                           annotation="Build sagging cables between the "
                                      "selected anchors.")
    clear = cmds.button(label="Clear Rig", height=38,
                        backgroundColor=(0.58, 0.30, 0.30),
                        command=_on_clear,
                        annotation="Delete every node this tool created.")
    cmds.formLayout(
        buttons, edit=True,
        attachForm=[(generate, "left", 0), (generate, "top", 0),
                    (generate, "bottom", 0), (clear, "right", 0),
                    (clear, "top", 0), (clear, "bottom", 0)],
        attachPosition=[(generate, "right", 3, 60), (clear, "left", 3, 60)])
    cmds.setParent(footer)

    cmds.button(label="Reset Settings", height=24, command=_on_reset)
    cmds.scrollField(CTRL_STATUS, editable=False, wordWrap=True, height=68,
                     text="[INFO] Ready. Select 2 or more objects, then "
                          "Generate.",
                     annotation="Status and auto-fit report for the last "
                                "run.")
    cmds.separator(height=6, style="none")
    cmds.setParent(form)

    cmds.formLayout(
        form, edit=True,
        attachForm=[(body, "top", 4), (body, "left", 4), (body, "right", 4),
                    (footer, "left", 0), (footer, "right", 0),
                    (footer, "bottom", 4)],
        attachControl=[(body, "bottom", 4, footer)])

    cmds.showWindow(window)
    return window


# ---------------------------------------------------------------
# 11. REMOTE LAUNCHER
# ---------------------------------------------------------------
# Convenience helper to open Maya's command port from inside Maya,
# so the terminal workflow below has something to connect to.
def open_command_port(port=MAYA_PORT, host=MAYA_HOST):
    if not MAYA_AVAILABLE:
        raise RuntimeError("open_command_port() must be run inside Maya.")

    name = "{0}:{1}".format(host, port)
    if cmds.commandPort(name, query=True):
        print("[CableHoseRigTool] Command port already open on " + name)
        return name

    cmds.commandPort(name=name, sourceType="mel", echoOutput=False)
    print("[CableHoseRigTool] Command port opened on " + name)
    return name


# Wrap the whole script in one MEL python("...") call.
#
# The source is Base64-encoded first. That is not decoration: the
# encoded text contains no quotes, backslashes or newlines, so it
# cannot break out of the MEL string literal no matter what the
# Python source contains.
def _build_payload(script_text):
    import base64

    encoded = base64.b64encode(script_text.encode("utf-8")).decode("ascii")

    python_code = (
        "import base64;"
        "_src = base64.b64decode('{0}').decode('utf-8');"
        "_ns = {{'__name__': '__maya_remote__'}};"
        "exec(compile(_src, 'CableHoseRigTool.py', 'exec'), _ns);"
        "_ns['show_ui']()"
    ).format(encoded)

    return 'python("{0}");\n'.format(python_code)


# Post this file to a running Maya over TCP. On failure it prints
# the exact commands needed to open the port, because a refused
# connection almost always means that step was skipped.
def send_to_maya(host=MAYA_HOST, port=MAYA_PORT, script_path=None,
                 timeout=10.0):
    import socket

    if script_path is None:
        script_path = os.path.abspath(__file__)

    if not os.path.isfile(script_path):
        print("ERROR: cannot find script file: " + script_path)
        return False

    with open(script_path, "r", encoding="utf-8") as handle:
        script_text = handle.read()

    payload = _build_payload(script_text)

    print("-" * 66)
    print("Cable & Hose Rig Tool - remote launcher")
    print("  script : {0}".format(script_path))
    print("  target : {0}:{1}".format(host, port))
    print("  bytes  : {0}".format(len(payload)))
    print("-" * 66)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)

    try:
        sock.connect((host, port))
        sock.sendall(payload.encode("utf-8"))

        try:
            reply = sock.recv(8192).decode("utf-8", errors="replace").strip()
            if reply:
                print("Maya replied: " + reply)
        except socket.timeout:
            pass

        print("SUCCESS: script sent. The tool window should now be open "
              "in Maya.")
        return True

    except (socket.timeout, ConnectionRefusedError, OSError) as err:
        print("ERROR: could not reach Maya on {0}:{1}".format(host, port))
        print("       {0}".format(err))
        print("")
        print("Open the command port inside Maya first.")
        print("  MEL   :  commandPort -name \"{0}:{1}\" -sourceType \"mel\";"
              .format(host, port))
        print("  Python:  import maya.cmds as cmds")
        print("           cmds.commandPort(name=\"{0}:{1}\", "
              "sourceType=\"mel\")".format(host, port))
        return False

    finally:
        sock.close()


# Terminal options: --host, --port, --timeout.
def _parse_args(argv):
    import argparse

    parser = argparse.ArgumentParser(
        description="Send CableHoseRigTool.py to a running Maya session "
                    "via the command port."
    )
    parser.add_argument("--host", default=MAYA_HOST,
                        help="Maya command-port host (default: %(default)s)")
    parser.add_argument("--port", type=int, default=MAYA_PORT,
                        help="Maya command-port port (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="Socket timeout in seconds (default: %(default)s)")
    return parser.parse_args(argv)


# The fork in the road. Inside Maya, show the UI. Outside Maya, act
# as the remote launcher.
def main(argv=None):
    if MAYA_AVAILABLE:
        return show_ui()

    args = _parse_args(sys.argv[1:] if argv is None else argv)
    ok = send_to_maya(host=args.host, port=args.port, timeout=args.timeout)
    return 0 if ok else 1


# Entry point. The MAYA_AVAILABLE guard matters here: when Maya
# executes the source we posted, this block runs again, and without
# the check it would try to open another socket to itself.
if __name__ == "__main__":
    result = main()
    if not MAYA_AVAILABLE:
        sys.exit(result)

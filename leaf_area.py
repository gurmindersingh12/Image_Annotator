"""Convert a reference line to image scale and optionally count green pixels."""
from bisect import bisect_left
from contextlib import nullcontext
from fractions import Fraction
import math

from PIL import Image, ImageChops
from traits import _edges, _union_spans


def _number(value, label):
    try:
        if type(value) in (int, float) and math.isfinite(value):
            return float(value)
    except OverflowError:
        pass
    raise ValueError(f"{label} must be a finite number.")


def _point(value, width, height):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("Each image point must have an x and a y coordinate.")
    x, y = (_number(n, "Coordinate") for n in value)
    if not (0 <= x <= width and 0 <= y <= height):
        raise ValueError("Place the ruler and measurement region inside the image.")
    return [x, y]


def _scale_values(ruler, length, unit, width, height):
    pixels = math.dist(*ruler)
    if pixels < 1:
        raise ValueError("Place the ruler endpoints at least one image pixel apart.")
    cm = length / 10 if unit == "mm" else length
    cm_per_pixel = cm / pixels
    pixel_area = cm_per_pixel * cm_per_pixel
    if pixel_area == 0 or not math.isfinite(width * height * pixel_area):
        raise ValueError("The entered ruler length is outside the measurable range.")
    return pixels, cm, cm_per_pixel



def _turn(a, b, c):
    """Orientation sign with an exact fallback for nearly collinear coordinates."""
    if a[0] == b[0] == c[0] or a[1] == b[1] == c[1]:
        return 0
    u = (b[0] - a[0]) * (c[1] - a[1])
    v = (b[1] - a[1]) * (c[0] - a[0])
    cross = u - v
    if abs(cross) <= 1e-14 * (abs(u) + abs(v)):
        ax, ay = map(Fraction, a)
        bx, by = map(Fraction, b)
        cx, cy = map(Fraction, c)
        cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    return (cross > 0) - (cross < 0)


def is_simple_outline(points):
    """Check a ring of validated image coordinates for crossings or retracing."""
    ring = []
    for p in points:
        q = tuple(p)
        if not ring or q != ring[-1]:
            ring.append(q)
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    size = len(ring)
    if size < 3 or len(set(ring)) != size:
        return False
    boxes = []
    for i, b in enumerate(ring):
        a, c = ring[i - 1], ring[(i + 1) % size]
        if _turn(a, b, c) == 0:
            between = all(min(first, last) <= middle <= max(first, last)
                          for first, middle, last in zip(a, b, c))
            if not between:
                return False
        boxes.append((min(b[0], c[0]), min(b[1], c[1]), max(b[0], c[0]), max(b[1], c[1])))

    def overlaps_on_axis(axis):
        # The axis with fewer overlapping intervals shortens the active edge list.
        ends = sorted(box[axis + 2] for box in boxes)
        return sum(size - bisect_left(ends, box[axis]) for box in boxes)

    axis = 0 if overlaps_on_axis(0) <= overlaps_on_axis(1) else 1
    other = 1 - axis
    active = []
    for i in sorted(range(size), key=lambda k: boxes[k][axis]):
        box = boxes[i]
        active = [j for j in active if boxes[j][axis + 2] >= box[axis]]
        a, b = ring[i], ring[(i + 1) % size]
        for j in active:
            if abs(i - j) in (1, size - 1):
                continue
            other_box = boxes[j]
            if box[other] > other_box[other + 2] or other_box[other] > box[other + 2]:
                continue
            c, d = ring[j], ring[(j + 1) % size]
            if _turn(a, b, c) * _turn(a, b, d) <= 0 and _turn(c, d, a) * _turn(c, d, b) <= 0:
                return False
        active.append(i)
    return True


def normalize_spec(spec, width, height):
    """Return a validated, JSON-serializable spec in oriented source coordinates."""
    if not isinstance(spec, dict):
        raise ValueError("Supply the ruler endpoints, length and unit.")
    mode = spec.get("mode", "scale")
    if mode not in ("scale", "green"):
        raise ValueError("Choose scale only or green tissue measurement.")
    ruler = spec.get("ruler")
    if not isinstance(ruler, (list, tuple)) or len(ruler) != 2:
        raise ValueError("Mark both ends of a known ruler interval.")
    ruler = [_point(p, width, height) for p in ruler]
    length = _number(spec.get("length"), "Known ruler length")
    unit = spec.get("unit")
    if length <= 0 or unit not in ("mm", "cm"):
        raise ValueError("Enter a positive ruler length in mm or cm.")
    _scale_values(ruler, length, unit, width, height)
    threshold = _number(spec.get("threshold", 12), "Green threshold")
    if not threshold.is_integer() or not 1 <= threshold <= 100:
        raise ValueError("Choose a whole-number green threshold from 1 to 100.")
    roi = spec.get("roi")
    if roi is not None:
        if not isinstance(roi, (list, tuple)) or len(roi) != 4:
            raise ValueError("The crop needs two opposite corners.")
        left, top = _point(roi[:2], width, height)
        right, bottom = _point(roi[2:], width, height)
        if math.ceil(left - .5) >= math.ceil(right - .5) or math.ceil(top - .5) >= math.ceil(bottom - .5):
            raise ValueError("Choose a crop containing at least one pixel center.")
        roi = [left, top, right, bottom]
    outline = spec.get("outline")
    if outline is not None:
        if not isinstance(outline, (list, tuple)) or not 3 <= len(outline) <= 5000:
            raise ValueError("A leaf outline needs 3 to 5,000 points.")
        outline = [_point(p, width, height) for p in outline]
        if not is_simple_outline(outline):
            raise ValueError("Trace the outline once around the boundary without crossing or retracing an edge.")
    return {"mode": mode, "ruler": ruler, "length": length, "unit": unit,
            "threshold": int(threshold), "roi": roi, "outline": outline}


def _green_counts(image, spec):
    """Measure at native resolution with a bounded strip of working RGB data."""
    width, height = image.size
    outline = spec["outline"]
    if outline:
        xs, ys = zip(*outline)
        bounds = [min(xs), min(ys), max(xs), max(ys)]
        starts = _edges([outline], height)
    else:
        bounds = spec["roi"] or [0, 0, width, height]
    left, top, right, bottom = [math.ceil(x - .5) for x in bounds]
    lookup = [int(value >= spec["threshold"]) for value in range(256)]
    green_count = outline_count = 0
    active = []
    # The decoder holds the source image. Color masks use tiles of at most
    # 4096 x 128 pixels so the number of per-channel copies is bounded.
    for y0 in range(top, bottom, 128):
        y1 = min(bottom, y0 + 128)
        rows = []
        if outline:
            for y in range(y0, y1):
                active = [edge for edge in active if edge[0] > y]
                active.extend(starts.get(y, ()))
                spans = _union_spans(active, y, width)
                rows.append(spans)
                outline_count += sum(end - start for start, end in spans)
        for x0 in range(left, right, 4096):
            x1 = min(right, x0 + 4096)
            tile = image.crop((x0, y0, x1, y1))
            if tile.mode != "RGB":
                tile = tile.convert("RGB")
            red, green, blue = tile.split()
            dominance = ImageChops.subtract(green, ImageChops.lighter(red, blue))
            mask = dominance.point(lookup).tobytes()
            if not outline:
                green_count += mask.count(b"\x01")
                continue
            stride = x1 - x0
            for row_index, spans in enumerate(rows):
                base = row_index * stride
                for start, end in spans:
                    lo, hi = max(x0, start), min(x1, end)
                    if lo < hi:
                        green_count += mask.count(b"\x01", base + lo - x0, base + hi - x0)
    if outline and not outline_count:
        raise ValueError("The traced outline encloses no source-pixel centers.")
    return green_count, (outline_count if outline else None)


def measure_leaf_area(image, spec):
    """Measure a saved RGB image (a Pillow image or path); results use cm and cm²."""
    source = nullcontext(image) if isinstance(image, Image.Image) else Image.open(image)
    with source as im:
        spec = normalize_spec(spec, *im.size)
        px, cm, factor = _scale_values(spec["ruler"], spec["length"], spec["unit"], *im.size)
        green = outline = None
        if spec["mode"] == "green":
            green, outline = _green_counts(im, spec)
    pixel_area = factor * factor
    method = "green-dominance-v1" if spec["mode"] == "green" else "two-point-scale-v1"
    return {"method": method, "scale_px": px, "ruler_cm": cm, "cm_per_pixel": factor,
            "green_pixels": green, "green_area_cm2": None if green is None else green * pixel_area,
            "outline_pixels": outline, "outline_area_cm2": None if outline is None else outline * pixel_area}

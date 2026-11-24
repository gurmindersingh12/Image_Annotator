"""Image-based traits calculated from validated annotations in oriented pixel coordinates."""
from collections import defaultdict
from math import ceil


def _edges(polygons, height):
    """Group nonhorizontal edge starts by scanline for an even-odd polygon fill."""
    starts = defaultdict(list)
    for polygon_id, points in enumerate(polygons):
        x0, y0 = points[-1]
        for x1, y1 in points:
            if y0 != y1:
                first = max(0, ceil(min(y0, y1) - 0.5))
                stop = min(height, ceil(max(y0, y1) - 0.5))
                if first < stop:
                    slope = (x1 - x0) / (y1 - y0)
                    starts[first].append((stop, polygon_id, x0, y0, slope))
            x0, y0 = x1, y1
    return starts


def _union_spans(active, y, width):
    """Return disjoint half-open column spans whose pixel centers fall inside any polygon."""
    crossings = defaultdict(list)
    for stop, polygon_id, x0, y0, slope in active:
        crossings[polygon_id].append(x0 + (y + 0.5 - y0) * slope)
    intervals = []
    for xs in crossings.values():
        xs.sort()
        for left, right in zip(xs[::2], xs[1::2]):
            start = max(0, ceil(left - 0.5))
            end = min(width, ceil(right - 0.5))
            if start < end:
                intervals.append((start, end))
    combined = []
    for start, end in sorted(intervals):
        if combined and start <= combined[-1][1]:
            combined[-1][1] = max(combined[-1][1], end)
        else:
            combined.append([start, end])
    return combined


def _intersection_size(a, b):
    count = i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        count += max(0, hi - lo)
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return count


def _pixel_areas(leaves, damage, width, height):
    source = [_edges(polygons, height) for polygons in (leaves, damage)]
    events = sorted(set(source[0]) | set(source[1]))
    leaf_pixels = damage_pixels = overlap_pixels = 0
    active = [[], []]
    y = events[0] if events else height
    index = 0
    while y < height:
        if index < len(events) and events[index] == y:
            index += 1
        for edges, starts in zip(active, source):
            edges[:] = [edge for edge in edges if edge[0] > y]
            edges.extend(starts.get(y, ()))
        next_event = events[index] if index < len(events) else height
        if not any(active):
            y = next_event
            continue
        leaf_spans, damage_spans = (_union_spans(edges, y, width) for edges in active)
        # On vertical edges the row is unchanged until an edge starts or stops.
        rows = 1
        if all(edge[4] == 0 for edges in active for edge in edges):
            stop = min(edge[0] for edges in active for edge in edges)
            rows = min(next_event, stop) - y
        leaf_pixels += rows * sum(end - start for start, end in leaf_spans)
        damage_pixels += rows * sum(end - start for start, end in damage_spans)
        overlap_pixels += rows * _intersection_size(leaf_spans, damage_spans)
        y += rows
    return leaf_pixels, damage_pixels, overlap_pixels


def leaf_damage_result(annotations, width, height, leaf_class_id, damage_class_id, status):
    """Measure the fraction of traced leaf pixels covered by traced damaged tissue."""
    def result(qc, pct=None):
        return {"leaf_damage_pct": pct, "leaf_damage_qc": qc}

    if status == "unannotated":
        return result("unscored")
    leaves, damage = [], []
    roles = {leaf_class_id: leaves, damage_class_id: damage}
    for obj in annotations:
        if obj["class_id"] in roles:
            if obj["type"] != "polygon":
                return result("polygon_required")
            roles[obj["class_id"]].append(obj["points"])
    if not leaves:
        return result("missing_leaf_outline")
    leaf_pixels, damage_pixels, overlap_pixels = _pixel_areas(leaves, damage, width, height)
    if not leaf_pixels:
        return result("subpixel_leaf")
    reviewed = status == "reviewed"
    if damage:
        if not damage_pixels:
            return result("subpixel_damage")
        if not overlap_pixels:
            return result("outside_leaf")
    elif not reviewed:
        return result("awaiting_review")
    return result("reviewed" if reviewed else "provisional", 100 * overlap_pixels / leaf_pixels)

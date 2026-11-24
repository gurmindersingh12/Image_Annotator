"""Reproducible practice scans. Images and polygons are generated from the same leaf shapes."""
import io
import math
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont


WIDTH, HEIGHT = 768, 512
RULER_X, RULER_Y = 50, 460
PIXELS_PER_CM = 50
RULER_LENGTH_CM = 2


def _draw_ruler(image):
    """Draw a digital reference whose end tick centers define a known distance."""
    draw = ImageDraw.Draw(image)
    end = RULER_X + PIXELS_PER_CM * RULER_LENGTH_CM
    draw.rounded_rectangle((28, 433, end + 62, 496), radius=4,
                           fill=(247, 246, 237), outline=(103, 104, 96))
    ink = (36, 40, 38)
    font = ImageFont.load_default(size=17)
    draw.line((RULER_X, RULER_Y, end, RULER_Y), fill=ink, width=2)
    for mm in range(RULER_LENGTH_CM * 10 + 1):
        x = RULER_X + mm * PIXELS_PER_CM // 10
        tick = 18 if mm % 10 == 0 else 12 if mm % 5 == 0 else 7
        draw.line((x, RULER_Y - tick, x, RULER_Y), fill=ink, width=1)
        if mm % 10 == 0:
            draw.text((x, RULER_Y + 5), str(mm // 10), anchor="mt", font=font, fill=ink)
    draw.text((end + 20, RULER_Y + 5), "cm", anchor="lt", font=font, fill=ink)


def _specimen(index, patch_count):
    rng = random.Random(7301 + index)
    center_x, center_y = 384 + rng.randrange(-12, 13), 252 + rng.randrange(-9, 10)
    angle = -.25 + rng.uniform(-.17, .14)
    length, breadth = 526 + rng.randrange(-16, 20), 152 + rng.randrange(-11, 14)
    phase = rng.uniform(-1, 1)

    def position(t, across):
        # t runs from the blade base to the tip, across is signed distance to the midrib.
        x = (t - .5) * length
        y = across + 13 * math.sin(t * math.pi + phase)
        return [round(center_x + x * math.cos(angle) - y * math.sin(angle)),
                round(center_y + x * math.sin(angle) + y * math.cos(angle))]

    def radius(t, side):
        base = breadth * max(0, math.sin(t * math.pi)) ** .90
        return base * (1.22 - .52 * t) * (1 + .026 * math.sin(21 * t + phase + side))

    outline = [position(i / 50, radius(i / 50, 1)) for i in range(51)]
    outline += [position(i / 50, -radius(i / 50, -1)) for i in range(49, 0, -1)]
    patches = []
    patch_sites = [(.34,-.48), (.59,.43), (.73,-.29), (.43,.58), (.22,.22)]
    for t, h in patch_sites[:patch_count]:
        rx, ry = rng.uniform(.025, .045), rng.uniform(8, 16)
        points = []
        for j in range(15):
            theta = math.tau * j / 15
            roughness = rng.uniform(.83, 1.14)
            points.append(position(t + rx * math.cos(theta) * roughness,
                                   h * radius(t, h) + ry * math.sin(theta) * roughness))
        patches.append(points)

    # Neutral specimen-board background and deterministic tonal variation in the blade.
    image = Image.new("RGB", (WIDTH, HEIGHT), (219, 221, 211))
    shadow = Image.new("L", image.size, 0)
    ImageDraw.Draw(shadow).polygon([(x+3,y+6) for x,y in outline], fill=58)
    image.paste((49,61,48), (0,0,WIDTH,HEIGHT), shadow.filter(ImageFilter.GaussianBlur(5)))
    draw = ImageDraw.Draw(image)
    draw.line([tuple(position(-.092, 3)), tuple(position(0,0)), tuple(position(.04,0))],
              fill=(112,119,60), width=7)
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).polygon([tuple(p) for p in outline], fill=255)
    # A small seeded texture field is interpolated to the image grid.
    texture = Image.new("L", (96, 64))
    texture.putdata([rng.randrange(225) for _ in range(96*64)])
    texture = texture.resize(image.size, Image.Resampling.BILINEAR)
    gradient = Image.linear_gradient("L").resize(image.size)
    texture = Image.blend(texture, gradient, .49)
    leaf = Image.merge("RGB", (
        texture.point(lambda value: round(52 + value * .22)),
        texture.point(lambda value: round(81 + value * .27)),
        texture.point(lambda value: round(37 + value * .15)),
    ))
    image.paste(leaf, mask=mask)
    draw = ImageDraw.Draw(image)
    for side in (1, -1):
        for i in range(2, 14):
            start = .012 + i * .061
            vein = []
            for j in range(15):
                f = j / 14
                t = start + .147 * math.sin(f * math.pi / 2)
                vein.append(tuple(position(t, side * radius(t, side) * f * .93)))
            draw.line(vein, fill=(113,139,76), width=2)
            for j in (5, 9):
                t = start + .147 * math.sin((j / 14) * math.pi / 2)
                endpoint = position(t + .048, side * radius(t + .048, side) * (j / 14 + .10))
                draw.line([vein[j], tuple(endpoint)], fill=(95,124,63), width=1)
    draw.line([tuple(position(t/80, 0)) for t in range(81)], fill=(158,165,103), width=3)
    for patch in patches:
        draw.polygon([tuple(p) for p in patch], fill=(170,115,55), outline=(132,93,47), width=2)
        cx = sum(p[0] for p in patch) / len(patch)
        cy = sum(p[1] for p in patch) / len(patch)
        inner = [(cx + (x-cx)*.68, cy + (y-cy)*.68) for x,y in patch]
        draw.polygon(inner, fill=(182,137,72))
    _draw_ruler(image)
    encoded = io.BytesIO()
    image.save(encoded, format="PNG", optimize=True)
    return encoded.getvalue(), outline, patches


def demo_images():
    """Yield six study records. Pending scans contain visible material for a user to trace."""
    statuses = ("reviewed", "reviewed", "annotated", "in_progress", "unannotated", "unannotated")
    for i, (status, n_patches) in enumerate(zip(statuses, (3, 0, 4, 2, 2, 5))):
        sample = f"SB-{101 + i // 2}"
        raw, outline, patches = _specimen(i, n_patches)
        polygons = []
        if status != "unannotated":
            polygons.append({"role": "leaf", "points": outline})
            visible = patches[:1] if status == "in_progress" else patches
            polygons.extend({"role": "damage", "points": p} for p in visible)
        calibration = None
        if status == "reviewed":
            calibration = {"mode": "green", "ruler": [[RULER_X, RULER_Y],
                           [RULER_X + PIXELS_PER_CM * RULER_LENGTH_CM, RULER_Y]],
                           "length": RULER_LENGTH_CM, "unit": "cm", "threshold": 12,
                           "roi": None, "outline": outline}
        yield {"name": f"{sample}_view-{i % 2 + 1:02}.png", "png": raw, "polygons": polygons,
               "sample_id": sample, "cultivar": ("Line A", "Line B", "Line C")[i // 2],
               "replicate": str(i // 2 + 1), "collection_date": "2026-06-17",
               "status": status, "calibration": calibration}


def create_demo(store):
    project = store.create_project("Soybean leaf survey",
                                   "Synthetic soybean leaflets with a 2 cm scale. Open a source image to inspect the blade outline or trace a new one.",
                                   preset="leaf_damage")
    roles = {role: item["id"] for role,item in zip(("leaf", "damage"), project["classes"])}
    for row in demo_images():
        image_id = store.upload(project["id"], row["name"], row["png"])["id"]
        revision = 0
        if row["calibration"] is not None:
            measured = store.set_leaf_area(image_id, {"revision": revision, "spec": row["calibration"]})
            revision = measured["revision"]
        annotations = [{"type": "polygon", "class_id": roles[shape["role"]], "points": shape["points"]}
                       for shape in row["polygons"]]
        record = {key: row[key] for key in ("sample_id", "cultivar", "replicate", "collection_date", "status")}
        store.save(image_id, {**record, "revision": revision, "annotations": annotations})
    return store.project(project["id"])

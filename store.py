"""Local data, annotation validation, and portable dataset exchange."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import random
import re
import sqlite3
import uuid
import warnings
import zipfile
from collections import Counter
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath

import yaml
from PIL import Image, ImageOps

from ag_demo import create_demo
from leaf_area import is_simple_outline, measure_leaf_area, normalize_spec
from traits import leaf_damage_result

APP_NAME = "Image Annotator"
APP_SLUG = "ImageAnnotator"
VERSION = "1.0.2"
MANIFEST_FILE = "imageannotator.json"
_KEEP_AREA = object()
SAMPLE_FIELDS = ("sample_id", "cultivar", "replicate", "collection_date")
RULER_COLUMNS = ("ruler_x1_px", "ruler_y1_px", "ruler_x2_px", "ruler_y2_px",
                 "ruler_length", "ruler_unit", "ruler_span_px", "cm_per_pixel")
AREA_COLUMNS = ("green_pixels", "green_area_cm2", "outline_pixels", "outline_area_cm2")
PRESETS = {
    "custom": ["Object"],
    "seed_count": ["Seed"],
    "root_nodules": ["Nodule"],
    "leaf_damage": ["Leaf outline", "Damaged tissue"],
    "calibrated_leaf": ["Leaf"],
}
COLORS = ["#b1e457", "#60a5fa", "#fb923c", "#c084fc", "#f472b6", "#2dd4bf", "#facc15", "#f87171"]
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
MAX_UPLOAD = 100 * 1024 * 1024
MAX_ARCHIVE = 1024 * 1024 * 1024
MAX_ZIP_EXPANDED = 4 * 1024 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 100_000_000


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat()


def ident():
    return uuid.uuid4().hex


def basename(name):
    name = str(name).replace("\\", "/").split("/")[-1]
    return re.sub(r"[\x00-\x1f]", "", name).strip()[:240] or "image.png"


def sample_fields(payload, previous):
    result = {}
    for field in SAMPLE_FIELDS:
        value = payload.get(field, previous.get(field, ""))
        if not isinstance(value, str) or len(value.strip()) > 160:
            raise Problem(f"{field} must be text of up to 160 characters.")
        result[field] = value.strip()
    day = result["collection_date"]
    if day:
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError()
        except ValueError:
            raise Problem("Use a valid collection date in YYYY-MM-DD format.")
    return result


def record_history(db, previous, stamp):
    # A sample sheet records history directly from its open SQLite transaction.
    if isinstance(previous.get("leaf_area"), str):
        previous = {**previous, "leaf_area": json.loads(previous["leaf_area"])}
    db.execute("INSERT INTO history (image_id,revision,snapshot,created_at) VALUES (?,?,?,?)",
               (previous["id"], previous["revision"], json.dumps(previous), stamp))
    db.execute("DELETE FROM history WHERE image_id=? AND revision < ?",
               (previous["id"], max(0, previous["revision"] - 29)))


def csv_text(value):
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def read_measurement(row, object_count):
    """Decode the stored measurement and expose review state for the current image revision."""
    area = json.loads(row["leaf_area"]) if row["leaf_area"] else None
    row["leaf_area"] = area
    row["area_only"] = bool(area and area["spec"]["mode"] == "green" and not object_count)
    has_area = area and (object_count or row["area_only"])
    row["area_qc"] = "unmeasured" if not has_area else ("reviewed" if row["status"] == "reviewed" else "provisional")
    # A calibration-only, pending, or green-only observation has no confirmed zero count.
    count_known = not row["area_only"] and (object_count or row["status"] in {"annotated", "reviewed"})
    row["measured_object_count"] = object_count if count_known else None


def ruler_values(area):
    if not area:
        return [None] * len(RULER_COLUMNS)
    spec = area["spec"]
    a, b = spec["ruler"]
    return [*a, *b, spec["length"], spec["unit"], area["scale_px"], area["cm_per_pixel"]]


def measurement_spec(snapshot):
    area = snapshot.get("leaf_area")
    if area is None:
        return None
    if not isinstance(area, dict) or not isinstance(area.get("spec"), dict):
        raise Problem("Invalid saved scale or leaf-area configuration.")
    return area["spec"]


def annotation_rgb(source):
    """Orient an image and map high-depth grayscale scans to 8-bit display pixels."""
    image = ImageOps.exif_transpose(source)
    if image.mode not in {"I", "F"} and not image.mode.startswith("I;16"):
        return image.convert("RGB")

    if image.mode.startswith("I;16"):
        # Pillow's integer conversion handles either byte order without clipping.
        image = image.convert("I")
        low, high = image.getextrema()
        if low == high:
            return Image.new("RGB", image.size, "black")
        # A 16-bit lookup table keeps large scanner images out of a Python pixel loop.
        ramp = [round(value * 255 / (high - low)) for value in range(high - low + 1)]
        lut = [0] * low + ramp + [255] * (65535 - high)
        return image.point(lut, "L").convert("RGB")
    sample_type = "f" if image.mode == "F" else "i"
    if image.mode == "I" and source.format == "TIFF":
        # The decoder also places unsigned 32-bit TIFF samples in mode I.
        if source.tag_v2.get(258) == (32,) and source.tag_v2.get(339, (1,)) == (1,):
            sample_type = "I"
    pixels = memoryview(image.tobytes()).cast(sample_type)
    if sample_type == "f" and not all(map(math.isfinite, pixels)):
        raise Problem("This grayscale image contains NaN or infinity. Export a finite display image before annotation.")
    low, high = min(pixels), max(pixels)
    if low == high:
        return Image.new("RGB", image.size, "black")
    # Keep the subtraction in native sample precision, including close 32-bit integers.
    span = high - low
    display = bytes(round((sample - low) * 255 / span) for sample in pixels)
    return Image.frombytes("L", image.size, display).convert("RGB")


def polygon_area(points):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))) / 2


def bounds(annotation):
    if annotation["type"] == "box":
        return [annotation[k] for k in ("x", "y", "w", "h")]
    xs, ys = zip(*annotation["points"])
    return [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]


def validate_annotations(items, width, height, class_ids):
    if not isinstance(items, list) or len(items) > 50000:
        raise Problem("Annotations must be a list of up to 50,000 objects.")
    output, seen = [], set()
    def number(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise Problem("Coordinates must be finite numbers.")
        return float(v)
    for item in items:
        if not isinstance(item, dict) or item.get("class_id") not in class_ids:
            raise Problem("Every object needs an existing class.")
        obj = {"id": str(item.get("id") or ident())[:80], "class_id": item["class_id"], "type": item.get("type")}
        if obj["id"] in seen:
            raise Problem("Object identifiers must be unique within an image.")
        seen.add(obj["id"])
        if obj["type"] == "box":
            try:
                x, y, w, h = [number(item[k]) for k in ("x", "y", "w", "h")]
            except KeyError:
                raise Problem("Incomplete bounding box.")
            if min(x, y) < -0.01 or w < 1 or h < 1 or x + w > width + .01 or y + h > height + .01:
                raise Problem("Boxes must be inside the image and at least 1 pixel wide and high.")
            obj.update(x=max(0, x), y=max(0, y), w=min(w, width - max(0, x)), h=min(h, height - max(0, y)))
        elif obj["type"] == "polygon":
            points = item.get("points")
            if not isinstance(points, list) or not 3 <= len(points) <= 5000:
                raise Problem("Polygons need 3 to 5,000 vertices.")
            if any(not isinstance(p, list) or len(p) != 2 for p in points):
                raise Problem("Invalid polygon vertex.")
            points = [[number(p[0]), number(p[1])] for p in points]
            if any(x < 0 or y < 0 or x > width or y > height for x, y in points):
                raise Problem("Polygon vertices must be inside the image.")
            if polygon_area(points) < 1:
                raise Problem("Polygon area must be at least 1 pixel.")
            obj["points"] = points
        else:
            raise Problem("Supported annotation types are box and polygon.")
        output.append(obj)
    return output


def workspace_database(root):
    """Identify an annotation database by its schema within the chosen data directory."""
    default = root / "imageannotator.sqlite3"
    if default.exists():
        return default
    columns = {
        "projects": {"id", "name", "description", "created_at"},
        "classes": {"id", "project_id", "name", "color", "ordinal"},
        "images": {"id", "project_id", "name", "file", "sha", "width", "height", "annotations",
                   "status", "notes", "split", "revision", "created_at", "updated_at"},
        "history": {"image_id", "revision", "snapshot", "created_at"},
    }
    matches = []
    for candidate in root.iterdir():
        if candidate.suffix.lower() not in {".db", ".sqlite", ".sqlite3"} or candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            with closing(sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True)) as db:
                if all(required <= {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                       for table, required in columns.items()):
                    matches.append(candidate)
        except sqlite3.DatabaseError:
            continue
    if len(matches) > 1:
        raise Problem("This folder contains multiple annotation databases. Choose a workspace with a single database.")
    return matches[0] if matches else default


def archive_metadata(archive, names):
    """Read a native manifest by schema, or collect the COCO documents in the ZIP."""
    json_files = [n for n in names if n.lower().endswith(".json")]
    native_files = [n for n in json_files if PurePosixPath(n).name == MANIFEST_FILE]
    manifest, coco_files = None, []
    for name in native_files or json_files:
        doc = json.loads(archive.read(name))
        if isinstance(doc, dict):
            project = doc.get("project")
            if (isinstance(project, dict) and isinstance(project.get("classes"), list)
                    and isinstance(doc.get("images"), list) and isinstance(doc.get("images_included"), bool)):
                if manifest is not None:
                    raise Problem("Import one project backup at a time.")
                manifest = doc
                continue
            if all(k in doc for k in ("images", "annotations", "categories")):
                coco_files.append((name, doc))
        if native_files:
            raise Problem("The project manifest is missing its classes, images, or image-inclusion flag.")
    return manifest, coco_files


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "images").mkdir(exist_ok=True)
        (self.root / "thumbnails").mkdir(exist_ok=True)
        self.database = workspace_database(self.root)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS classes (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL COLLATE NOCASE, color TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    UNIQUE(project_id, name));
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL, file TEXT NOT NULL, sha TEXT NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unannotated', split TEXT NOT NULL DEFAULT 'unassigned',
                    annotations TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(project_id, sha));
                CREATE INDEX IF NOT EXISTS images_project ON images(project_id);
                CREATE TABLE IF NOT EXISTS history (
                    image_id TEXT NOT NULL REFERENCES images(id), revision INTEGER NOT NULL,
                    snapshot TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(image_id, revision));
            """)

            # Serialize schema inspection so two starters can safely open the same folder.
            db.execute("BEGIN IMMEDIATE")
            columns = {
                "projects": {"preset": "TEXT NOT NULL DEFAULT 'custom'"},
                "classes": {"role": "TEXT NOT NULL DEFAULT ''"},
                "images": {
                    **{name: "TEXT NOT NULL DEFAULT ''" for name in SAMPLE_FIELDS},
                    "observation_id": "TEXT NOT NULL DEFAULT ''",
                    "leaf_damage_pct": "REAL",
                    "leaf_damage_qc": "TEXT NOT NULL DEFAULT 'unscored'",
                    "leaf_area": "TEXT",
                },
            }
            for table, additions in columns.items():
                existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                for name, definition in additions.items():
                    if name not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            db.execute("UPDATE images SET observation_id=id WHERE observation_id=''")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS observation_key ON images(project_id, observation_id)")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def project(self, project_id):
        with self.db() as db:
            p = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not p:
                raise Problem("Project not found.", 404)
            p = dict(p)
            p["classes"] = [dict(c) for c in db.execute("SELECT * FROM classes WHERE project_id=? ORDER BY ordinal", (project_id,))]
            rows = db.execute("SELECT status, split, annotations FROM images WHERE project_id=?", (project_id,)).fetchall()
        p["stats"] = {"total": len(rows), "objects": 0, "unannotated": 0, "in_progress": 0, "annotated": 0, "reviewed": 0}
        counts = {c["id"]: 0 for c in p["classes"]}
        for row in rows:
            p["stats"][row["status"]] += 1
            for obj in json.loads(row["annotations"]):
                p["stats"]["objects"] += 1
                counts[obj["class_id"]] = counts.get(obj["class_id"], 0) + 1
        p["class_counts"] = counts
        return p

    def projects(self):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM projects ORDER BY created_at DESC")]
        return [self.project(i) for i in ids]

    def create_project(self, name, description="", classes=None, preset="custom"):
        name, description = str(name).strip()[:120], str(description).strip()[:1000]
        if not name:
            raise Problem("Give the project a name.")
        if not isinstance(preset, str) or preset not in PRESETS:
            raise Problem("Unknown project preset.")
        if preset == "leaf_damage" and classes and classes[:2] != PRESETS[preset]:
            raise Problem("A leaf damage project starts with Leaf outline and Damaged tissue.")
        classes = [str(c).strip()[:80] for c in (classes or PRESETS[preset])]
        if len(classes) > 1000 or any(not c for c in classes) or len({c.casefold() for c in classes}) != len(classes):
            raise Problem("Class names must be nonempty and unique (up to 1,000 classes).")
        project_id = ident()
        with self.db() as db:
            db.execute("INSERT INTO projects (id,name,description,created_at,preset) VALUES (?,?,?,?,?)", (project_id, name, description, now(), preset))
            for i, c in enumerate(classes):
                role = ("leaf_outline", "damaged_tissue")[i] if preset == "leaf_damage" and i < 2 else ""
                db.execute("INSERT INTO classes (id,project_id,name,color,ordinal,role) VALUES (?,?,?,?,?,?)",
                           (ident(), project_id, c, COLORS[i % len(COLORS)], i, role))
        return self.project(project_id)

    def add_class(self, project_id, name):
        p = self.project(project_id)
        name = str(name).strip()[:80]
        if not name:
            raise Problem("Enter a class name.")
        ordinal = max([c["ordinal"] for c in p["classes"]], default=-1) + 1
        try:
            with self.db() as db:
                db.execute("INSERT INTO classes (id,project_id,name,color,ordinal) VALUES (?,?,?,?,?)", (ident(), project_id, name, COLORS[ordinal % len(COLORS)], ordinal))
        except sqlite3.IntegrityError:
            raise Problem("That class name already exists.")
        return self.project(project_id)

    def update_class(self, project_id, class_id, name, color):
        p = self.project(project_id)
        if class_id not in {c["id"] for c in p["classes"]}:
            raise Problem("Class not found.", 404)
        name = str(name).strip()[:80]
        if not name or not re.fullmatch(r"#[0-9a-fA-F]{6}", str(color)):
            raise Problem("Enter a class name and hex color.")
        try:
            with self.db() as db:
                db.execute("UPDATE classes SET name=?,color=? WHERE id=? AND project_id=?", (name, color, class_id, project_id))
        except sqlite3.IntegrityError:
            raise Problem("That class name already exists.")
        return self.project(project_id)

    def images(self, project_id):
        with self.db() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise Problem("Project not found.", 404)
            rows = db.execute("""
                SELECT id,observation_id,name,width,height,status,split,annotations,revision,notes,
                       sample_id,cultivar,replicate,collection_date,leaf_damage_pct,leaf_damage_qc,leaf_area
                FROM images WHERE project_id=? ORDER BY created_at,id
            """, (project_id,)).fetchall()
        result = []
        for row in rows:
            image = dict(row)
            annotations = json.loads(image.pop("annotations"))
            image["count"] = len(annotations)
            read_measurement(image, len(annotations))
            image["class_counts"] = dict(Counter(obj["class_id"] for obj in annotations))
            if image["status"] == "reviewed":
                image["qc"] = "reviewed" if annotations or image["area_only"] else "confirmed_zero"
            elif image["status"] == "unannotated":
                image["qc"] = "pending"
            else:
                image["qc"] = "provisional"
            result.append(image)
        return result

    def observation_csv(self, project_id, scope="all"):
        if scope not in {"all", "reviewed"}:
            raise Problem("Select all images or reviewed images.")
        p = self.project(project_id)
        images = self.images(project_id)
        grouped_splits = {}
        for image in images:
            if image["sample_id"]:
                grouped_splits.setdefault(image["sample_id"].casefold(), set()).add(image["split"])
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["project_id", "project", "observation_id", "image_id", "image", *SAMPLE_FIELDS,
                         "split", "sample_split_qc", "status", "qc", "leaf_damage_pct", "leaf_damage_qc", "notes",
                         "measurement_mode", "measurement_method", "green_threshold", "area_qc",
                         *RULER_COLUMNS, *AREA_COLUMNS,
                         *["count:" + c["name"] for c in p["classes"]]])
        for image in images:
            if scope == "reviewed" and image["status"] != "reviewed":
                continue
            sample = image["sample_id"].casefold()
            if not sample:
                split_qc = "missing_sample_id"
            elif len({"train" if value == "unassigned" else value for value in grouped_splits[sample]}) > 1:
                # Unassigned views become train images in training-format exports.
                split_qc = "mixed_splits"
            elif "unassigned" in grouped_splits[sample]:
                split_qc = "unassigned"
            else:
                split_qc = "grouped"
            counts = image["class_counts"]
            missing = image["measured_object_count"] is None
            is_leaf = p["preset"] == "leaf_damage"
            area = image["leaf_area"]
            mode = area["spec"]["mode"] if area else ""
            writer.writerow([p["id"], csv_text(p["name"]), image["observation_id"], image["id"], csv_text(image["name"]),
                             *[csv_text(image[key]) for key in SAMPLE_FIELDS], image["split"], split_qc,
                             image["status"], image["qc"], image["leaf_damage_pct"] if is_leaf else "",
                             image["leaf_damage_qc"] if is_leaf else "", csv_text(image["notes"]),
                             mode, area["method"] if area else None,
                             area["spec"]["threshold"] if mode == "green" else None, image["area_qc"], *ruler_values(area),
                             *[area[key] if area else None for key in AREA_COLUMNS],
                             *["" if missing else counts.get(c["id"], 0) for c in p["classes"]]])
        return output.getvalue().encode("utf-8-sig")

    def object_area_csv(self, project_id, scope="all"):
        if scope not in {"all", "reviewed"}:
            raise Problem("Select all images or reviewed images.")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["project_id", "observation_id", "image_id", "image", *SAMPLE_FIELDS,
                         "object_id", "class_id", "class", "shape_type", "area_px2", "area_cm2",
                         "status", "area_qc", *RULER_COLUMNS])
        with self.db() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise Problem("Project not found.", 404)
            classes = dict(db.execute("SELECT id,name FROM classes WHERE project_id=?", (project_id,)))
            rows = db.execute("""SELECT id,name,observation_id,sample_id,cultivar,replicate,collection_date,
                                        status,leaf_area,annotations FROM images
                                 WHERE project_id=? ORDER BY created_at,id""", (project_id,))
            for row in rows:
                if scope == "reviewed" and row["status"] != "reviewed":
                    continue
                image = dict(row)
                objects = json.loads(row["annotations"])
                read_measurement(image, len(objects))
                area = image["leaf_area"]
                square_cm = area["cm_per_pixel"] ** 2 if area else None
                for obj in objects:
                    qc = image["area_qc"]
                    if obj["type"] == "box":
                        pixels = obj["w"] * obj["h"]
                    elif is_simple_outline(obj["points"]):
                        pixels = polygon_area(obj["points"])
                    else:
                        pixels, qc = None, "crossed_outline"
                    writer.writerow([project_id, row["observation_id"], row["id"], csv_text(row["name"]),
                                     *[csv_text(row[key]) for key in SAMPLE_FIELDS], csv_text(obj["id"]),
                                     obj["class_id"], csv_text(classes[obj["class_id"]]), obj["type"],
                                     pixels, pixels * square_cm if area and pixels is not None else None,
                                     row["status"], qc, *ruler_values(area)])
        return output.getvalue().encode("utf-8-sig")

    def image(self, image_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        if not row:
            raise Problem("Image not found.", 404)
        row = dict(row)
        row["annotations"] = json.loads(row["annotations"])
        read_measurement(row, len(row["annotations"]))
        return row

    def upload(self, project_id, name, raw, *, observation_id=None):
        with self.db() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise Problem("Project not found.", 404)
        if len(raw) > MAX_UPLOAD:
            raise Problem("An image must be smaller than 100 MB.", 413)
        if not raw:
            raise Problem("The image is empty.")
        name = basename(name)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                original = Image.open(io.BytesIO(raw))
                if original.format not in {"JPEG", "PNG", "TIFF", "BMP", "WEBP"}:
                    raise Problem("Use PNG, JPEG, TIFF, BMP, or WebP images.")
                if getattr(original, "n_frames", 1) > 1:
                    raise Problem("Multi-page or animated images are not supported. Export each frame as a separate image.")
                exif_orientation = original.getexif().get(274, 1)
                im = annotation_rgb(original)
                im.load()
        except Problem:
            raise
        except Exception as exc:
            raise Problem("Unable to read this image. Check its format and size.") from exc
        # Hash canonical pixels, so identical images in different containers are detected.
        sha = hashlib.sha256(str(im.size).encode() + im.tobytes()).hexdigest()
        with self.db() as db:
            duplicate = db.execute("SELECT id FROM images WHERE project_id=? AND sha=?", (project_id, sha)).fetchone()
        if duplicate:
            return {"id": duplicate[0], "duplicate": True, "orientation_changed": exif_orientation not in (1, None)}
        image_id = ident()
        path = self.root / "images" / (image_id + ".png")
        im.save(path, compress_level=2)
        thumb = im.copy()
        thumb.thumbnail((460, 340))
        thumb.save(self.root / "thumbnails" / (image_id + ".jpg"), quality=85)
        stamp = now()
        try:
            with self.db() as db:
                db.execute("INSERT INTO images (id,project_id,name,file,sha,width,height,created_at,updated_at,observation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (image_id, project_id, name, path.name, sha, *im.size, stamp, stamp, observation_id or image_id))
        except sqlite3.IntegrityError:
            path.unlink(missing_ok=True)
            (self.root / "thumbnails" / (image_id + ".jpg")).unlink(missing_ok=True)
            with self.db() as db:
                duplicate = db.execute("SELECT id FROM images WHERE project_id=? AND sha=?", (project_id, sha)).fetchone()
            if duplicate:
                return {"id": duplicate[0], "duplicate": True, "orientation_changed": exif_orientation not in (1, None)}
            raise Problem("The observation ID already refers to different image pixels in this project.")
        return {"id": image_id, "duplicate": False, "orientation_changed": exif_orientation not in (1, None)}

    def set_leaf_area(self, image_id, payload):
        if "spec" not in payload:
            raise Problem("Supply a scale configuration or null to remove it.")
        return self.save(image_id, {"revision": payload.get("revision")},
                         area_spec=payload["spec"], editing_area=True)

    def save(self, image_id, payload, *, area_spec=_KEEP_AREA, editing_area=False):
        image = self.image(image_id)
        if payload.get("revision") != image["revision"]:
            raise Problem("This image changed in another tab. Reload it before saving; your current edits are still visible.", 409)
        area = image["leaf_area"]
        if area_spec is not _KEEP_AREA:
            try:
                spec = normalize_spec(area_spec, image["width"], image["height"]) if area_spec is not None else None
                if spec != measurement_spec(image):
                    area = ({"spec": spec, **measure_leaf_area(self.root / "images" / image["file"], spec)}
                            if spec is not None else None)
                elif editing_area:
                    return image
            except ValueError as exc:
                raise Problem(str(exc)) from exc
        with self.db() as db:
            class_rows = db.execute("SELECT id,role FROM classes WHERE project_id=?", (image["project_id"],)).fetchall()
        classes = {c["id"] for c in class_rows}
        roles = {c["role"]: c["id"] for c in class_rows if c["role"]}
        fields = sample_fields(payload, image)
        annotations = validate_annotations(payload.get("annotations", image["annotations"]), image["width"], image["height"], classes)
        status = payload.get("status", image["status"])
        has_green = area and area["spec"]["mode"] == "green"
        if editing_area:
            status = "in_progress" if has_green or annotations else "unannotated"
        elif (area_spec is _KEEP_AREA and area and image["status"] == status == "reviewed"
              and annotations != image["annotations"]):
            status = "in_progress"
        split = payload.get("split", image["split"])
        if status not in {"unannotated", "in_progress", "annotated", "reviewed"} or split not in {"unassigned", "train", "val", "test"}:
            raise Problem("Invalid image status or dataset split.")
        if status == "unannotated" and (annotations or has_green):
            raise Problem("An image with objects or a saved leaf measurement cannot be unannotated.")
        area_only = bool(has_green and not annotations)
        already_confirmed = image["status"] in {"annotated", "reviewed"} and not image["annotations"] and not image["area_only"]
        if status in {"annotated", "reviewed"} and not annotations and not (area_only or already_confirmed or payload.get("confirm_negative")):
            raise Problem("Confirm that this image contains no target objects before completing it.")
        notes = str(payload.get("notes", image["notes"]))[:5000]
        trait = {"leaf_damage_pct": image["leaf_damage_pct"], "leaf_damage_qc": image["leaf_damage_qc"]}
        if "leaf_outline" in roles and (annotations != image["annotations"] or status != image["status"]):
            trait = leaf_damage_result(annotations, image["width"], image["height"],
                                       roles["leaf_outline"], roles["damaged_tissue"], status)
        stamp = now()
        with self.db() as db:
            cur = db.execute("""
                UPDATE images SET annotations=?,status=?,split=?,notes=?,sample_id=?,cultivar=?,replicate=?,collection_date=?,
                                  leaf_damage_pct=?,leaf_damage_qc=?,leaf_area=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?
            """, (json.dumps(annotations), status, split, notes, *[fields[key] for key in SAMPLE_FIELDS],
                  trait["leaf_damage_pct"], trait["leaf_damage_qc"], json.dumps(area) if area else None,
                  stamp, image_id, image["revision"]))
            if cur.rowcount != 1:
                raise Problem("This image changed in another tab. Reload it before saving.", 409)
            record_history(db, image, stamp)
        return self.image(image_id)

    def history(self, image_id):
        self.image(image_id)
        with self.db() as db:
            return [{"revision": r[0], "created_at": r[1]} for r in db.execute("SELECT revision,created_at FROM history WHERE image_id=? ORDER BY revision DESC", (image_id,))]

    def restore_revision(self, image_id, revision, current_revision):
        with self.db() as db:
            row = db.execute("SELECT snapshot FROM history WHERE image_id=? AND revision=?", (image_id, revision)).fetchone()
        if not row:
            raise Problem("Saved revision not found.", 404)
        payload = json.loads(row[0])
        for field in SAMPLE_FIELDS:
            payload.setdefault(field, "")
        payload.update(revision=current_revision, confirm_negative=True)
        return self.save(image_id, payload, area_spec=measurement_spec(payload))

    def split(self, project_id, train=80, val=20, test=0, seed=42):
        values = [float(train), float(val), float(test)]
        if not all(math.isfinite(v) and 0 <= v <= 100 for v in values) or abs(sum(values) - 100) > .001:
            raise Problem("Train, validation, and test percentages must total 100.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,sha,sample_id,split FROM images WHERE project_id=? ORDER BY sha", (project_id,))
            groups = {}
            for row in rows:
                key = ("sample", row["sample_id"].casefold()) if row["sample_id"] else ("image", row["sha"])
                groups.setdefault(key, []).append(row)
            # Percentages apply to independent sample groups. Sorting the keys makes
            # the split reproducible regardless of upload order or local image IDs.
            keys = sorted(groups)
            random.Random(int(seed)).shuffle(keys)
            target = [len(keys) * percentage / 100 for percentage in values]
            sizes = [math.floor(count) for count in target]
            remainder = sorted(range(3), key=lambda i: target[i] - sizes[i], reverse=True)
            for i in remainder[:len(keys) - sum(sizes)]:
                sizes[i] += 1
            stamp = now()
            for index, key in enumerate(keys):
                split_name = "train" if index < sizes[0] else "val" if index < sizes[0] + sizes[1] else "test"
                db.executemany("UPDATE images SET split=?,revision=revision+1,updated_at=? WHERE id=?",
                               [(split_name, stamp, r["id"]) for r in groups[key] if r["split"] != split_name])
        return self.images(project_id)

    def demo(self):
        return create_demo(self)

    def export(self, project_id, fmt="yolo", scope="completed", include_images=True):
        if fmt not in {"yolo", "yolo-seg", "coco", "backup"}:
            raise Problem("Choose YOLO detection, YOLO segmentation, COCO, or backup.")
        if scope not in {"completed", "reviewed", "all"}:
            raise Problem("Invalid export scope.")
        p = self.project(project_id)
        rows = [self.image(i["id"]) for i in self.images(project_id)]
        if fmt != "backup":
            eligible = []
            for image in rows:
                complete = image["status"] in {"annotated", "reviewed"}
                if image["leaf_area"] and image["measured_object_count"] is None:
                    continue
                if scope == "all" or image["status"] == "reviewed" or (scope == "completed" and complete):
                    eligible.append(image)
            rows = eligible
        if not rows:
            raise Problem("No images match this export. Images measured only for green area are excluded from training datasets.")
        if fmt == "yolo-seg" and any(a["type"] != "polygon" for i in rows for a in i["annotations"]):
            raise Problem("Segmentation export requires polygons for every object. Use YOLO detection or COCO for bounding boxes.")
        mapping = {c["id"]: n for n, c in enumerate(p["classes"])}
        out = io.BytesIO()
        # Canonical PNG pixels are exported, matching the annotation coordinate system.
        manifest = {"app":APP_NAME, "software":APP_NAME, "version":VERSION, "created_at":now(), "project":p, "images":rows,
                    "format":fmt, "scope":scope, "coordinate_system":"pixels after EXIF orientation", "images_included": bool(include_images or fmt == "backup")}
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            counts = ["image,split,status,objects"]
            coco_sets = {s:{"info":{"description":p["name"],"version":VERSION},"images":[],"annotations":[],"categories":[{"id":mapping[c["id"]],"name":c["name"]} for c in p["classes"]]} for s in ("train","val","test")}
            annotation_id = 1
            for image_number, image in enumerate(rows, 1):
                split = image["split"] if image["split"] != "unassigned" else "train"
                stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", Path(image["name"]).stem)[:100] or "image"
                name = f"{stem}_{image['id']}.png"
                image_path = f"{split}/{name}" if fmt == "coco" else f"images/{split}/{name}"
                image["export_file"] = image_path
                if include_images or fmt == "backup":
                    archive.write(self.root / "images" / image["file"], image_path)
                label_lines = []
                for obj in image["annotations"]:
                    category = mapping[obj["class_id"]]
                    x, y, w, h = bounds(obj)
                    if fmt == "yolo-seg":
                        coords = [v/d for pt in obj["points"] for v,d in zip(pt,(image["width"],image["height"]))]
                    else:
                        coords = [(x+w/2)/image["width"],(y+h/2)/image["height"],w/image["width"],h/image["height"]]
                    label_lines.append(str(category) + " " + " ".join(f"{v:.8f}" for v in coords))
                    if fmt == "coco":
                        points = obj.get("points")
                        coco_sets[split]["annotations"].append({"id":annotation_id,"image_id":image_number,"category_id":category,"bbox":[x,y,w,h],
                            "area":polygon_area(points) if points else w*h,"iscrowd":0,"segmentation":[[v for pt in points for v in pt]] if points else []})
                        annotation_id += 1
                if fmt in {"yolo", "yolo-seg"}:
                    archive.writestr(f"labels/{split}/{Path(name).stem}.txt", "\n".join(label_lines) + ("\n" if label_lines else ""))
                if fmt == "coco":
                    coco_sets[split]["images"].append({"id":image_number,"file_name":name,"width":image["width"],"height":image["height"]})
                count = image["measured_object_count"] if image["measured_object_count"] is not None else ""
                counts.append(f'{name},{split},{image["status"]},{count}')
            if fmt in {"yolo", "yolo-seg"}:
                archive.writestr("data.yaml", yaml.safe_dump({"path":".","train":"images/train","val":"images/val","test":"images/test","nc":len(mapping),"names":{i:c["name"] for i,c in enumerate(p["classes"]) }}, sort_keys=False, allow_unicode=True))
            if fmt == "coco":
                for split, coco in coco_sets.items():
                    if coco["images"]:
                        archive.writestr(f"{split}/_annotations.coco.json", json.dumps(coco, indent=2))
            # Record export paths after every image has been assigned a unique name.
            manifest["images"] = rows
            archive.writestr(MANIFEST_FILE, json.dumps(manifest, indent=2))
            archive.writestr("counts.csv", "\n".join(counts) + "\n")
            archive.writestr("README.txt", f"Created with {APP_NAME}. Images use canonical RGB PNG pixels.\n"
                "Unassigned images export into train. Assign a split before training.\n"
                "For YOLO: extract, then set path in data.yaml to this extracted directory's absolute path.\n"
                "The default export includes completed images and confirmed negatives only.\n"
                "counts.csv contains object totals; imageannotator.json retains original names and class IDs.\n"
                "A backup is a ZIP with images plus imageannotator.json; import it to a NEW project to restore.\n"
                "The split tool keeps matching sample IDs together. Check missing IDs and manual split assignments.\n")
        return out.getvalue()

    def import_archive(self, project_id, raw):
        self.project(project_id)
        empty_project = not self.images(project_id)
        if len(raw) > MAX_ARCHIVE:
            raise Problem("ZIP imports must be smaller than 1 GB.", 413)
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise Problem("Choose a valid dataset ZIP.")
        with z:
            infos = z.infolist()
            if len(infos) > 50000 or sum(i.file_size for i in infos) > MAX_ZIP_EXPANDED:
                raise Problem("Archive is too large: limit 50,000 entries and 4 GB expanded.")
            names = [i.filename for i in infos if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
            if len(names) != len(set(names)):
                raise Problem("Archive contains duplicate paths.")
            if any(PurePosixPath(n.replace('\\','/')).is_absolute() or ".." in PurePosixPath(n.replace('\\','/')).parts for n in names):
                raise Problem("Archive contains unsafe paths.")
            image_names = [n for n in names if Path(n).suffix.lower() in IMAGE_EXTS]
            if not image_names:
                raise Problem("No supported images were found in the ZIP.")
            # Fully validate the label mapping before adding images.
            manifest, coco_files = archive_metadata(z, names)
            class_names, coco_by_path, yolo_by_path, backup_by_path = [], {}, {}, {}
            incoming_classes = {}
            if manifest is not None:
                class_names = [c["name"] for c in manifest["project"]["classes"]]
                incoming_classes = {c["id"]:c["name"] for c in manifest["project"]["classes"]}
                backup_by_path = {i["export_file"]:i for i in manifest["images"]}
                if not manifest.get("images_included"):
                    raise Problem("This export has no image files. Choose a full dataset or project backup.")
            else:
                if coco_files:
                    class_names = list(dict.fromkeys(c["name"] for _, doc in coco_files for c in doc["categories"]))
                    for coco_path, doc in coco_files:
                        category_names = {c["id"]:c["name"] for c in doc["categories"]}
                        annotations = {}
                        for a in doc["annotations"]:
                            if a.get("iscrowd",0) or isinstance(a.get("segmentation"),dict):
                                raise Problem("COCO RLE/crowd masks are unsupported. Convert them to polygons first.")
                            if len(a.get("segmentation",[])) > 1:
                                raise Problem("Multi-part COCO masks are unsupported. Convert them to individual polygons first.")
                            annotations.setdefault(a["image_id"],[]).append({**a,"_class_name":category_names[a["category_id"]]})
                        for im in doc["images"]:
                            file_name = im["file_name"].replace('\\','/')
                            candidates = [n for n in image_names if n == file_name or n == str(PurePosixPath(coco_path).parent / file_name)]
                            if not candidates:
                                candidates = [n for n in image_names if basename(n) == basename(file_name)]
                            if len(candidates) != 1:
                                raise Problem(f"Cannot match COCO image uniquely: {basename(file_name)}.")
                            coco_by_path[candidates[0]] = (im, annotations.get(im["id"],[]))
                else:
                    yaml_names = [n for n in names if n.lower().endswith((".yaml", ".yml"))]
                    classes_txt = [n for n in names if Path(n).name == "classes.txt"]
                    if yaml_names:
                        doc = yaml.safe_load(z.read(yaml_names[0]))
                        declared = doc.get("names",[]) if isinstance(doc,dict) else []
                        if isinstance(declared,dict):
                            try:
                                declared = {int(k):v for k,v in declared.items()}
                                if sorted(declared) != list(range(len(declared))):
                                    raise ValueError()
                                class_names = [declared[k] for k in sorted(declared)]
                            except (ValueError, TypeError):
                                raise Problem("YOLO class IDs in data.yaml must be contiguous, starting at 0.")
                        else:
                            class_names = list(declared)
                    elif classes_txt:
                        class_names = z.read(classes_txt[0]).decode("utf-8-sig").splitlines()
                    label_names = [n for n in names if n.endswith(".txt") and Path(n).name != "classes.txt"
                                   and not Path(n).name.casefold().startswith("readme.")]
                    if label_names and not class_names:
                        raise Problem("YOLO import needs data.yaml with names or classes.txt. Labels will not be guessed.")
                    for n in image_names:
                        pth = PurePosixPath(n)
                        exact = str(pth.with_suffix(".txt")).replace("/images/", "/labels/")
                        if exact.startswith("images/"):
                            exact = "labels/" + exact[len("images/"):]
                        same_directory = str(pth.with_suffix(".txt"))
                        matches = [k for k in label_names if k in {exact,same_directory}]
                        if not matches:
                            matches = [k for k in label_names if Path(k).stem == pth.stem]
                        if len(matches) > 1:
                            raise Problem(f"Ambiguous YOLO label match: {pth.name}.")
                        if matches:
                            lines = []
                            for line_number, line in enumerate(z.read(matches[0]).decode("utf-8-sig").splitlines(),1):
                                if not line.strip():
                                    continue
                                try:
                                    parts = line.split()
                                    cid = int(parts[0])
                                    coords = [float(v) for v in parts[1:]]
                                    if cid < 0 or cid >= len(class_names) or not all(math.isfinite(v) and 0 <= v <= 1 for v in coords):
                                        raise ValueError()
                                    if len(coords) != 4 and (len(coords) < 6 or len(coords) % 2):
                                        raise ValueError()
                                except (ValueError, IndexError):
                                    raise Problem(f"Invalid YOLO label in {basename(matches[0])}, line {line_number}.")
                                lines.append((class_names[cid], coords))
                            yolo_by_path[n] = lines
            if not all(isinstance(n,str) and n.strip() and len(n.strip()) <= 80 for n in class_names):
                raise Problem("Dataset class names must be nonempty strings of at most 80 characters.")
            for name in class_names:
                p = self.project(project_id)
                if name.casefold() not in {c["name"].casefold() for c in p["classes"]}:
                    self.add_class(project_id, name)
            p = self.project(project_id)
            mapping = {c["name"].casefold():c["id"] for c in p["classes"]}
            if backup_by_path and empty_project:
                restored_preset = manifest["project"].get("preset", "custom")
                if restored_preset not in PRESETS:
                    raise Problem("This archive uses an unsupported project preset.")
                restored_roles = {}
                if restored_preset == "leaf_damage":
                    for c in manifest["project"]["classes"]:
                        role = c.get("role")
                        if role in {"leaf_outline", "damaged_tissue"}:
                            if role in restored_roles:
                                raise Problem("This backup repeats a leaf measurement class role.")
                            restored_roles[role] = mapping[c["name"].casefold()]
                    if len(restored_roles) != 2:
                        raise Problem("This backup omits a leaf measurement class role.")
                with self.db() as db:
                    db.execute("UPDATE projects SET preset=? WHERE id=?", (restored_preset, project_id))
                    db.execute("UPDATE classes SET role='' WHERE project_id=?", (project_id,))
                    for role, class_id in restored_roles.items():
                        db.execute("UPDATE classes SET role=? WHERE id=?", (role, class_id))
                for c in manifest["project"]["classes"]:
                    self.update_class(project_id, mapping[c["name"].casefold()], c["name"], c["color"])
            result = {"added":0,"duplicates":0,"labeled":0,"errors":[]}
            for n in image_names:
                uploaded = None
                try:
                    if z.getinfo(n).file_size > MAX_UPLOAD:
                        raise Problem("Image exceeds 100 MB.")
                    backup = backup_by_path.get(n)
                    source_id = None
                    if backup:
                        source_id = backup.get("observation_id") or backup.get("id")
                        if source_id is not None and not re.fullmatch(r"[a-f0-9]{32}", str(source_id)):
                            raise Problem("The backup has an invalid observation ID.")
                    uploaded = self.upload(project_id, backup["name"] if backup else basename(n), z.read(n), observation_id=source_id)
                    if uploaded["duplicate"]:
                        result["duplicates"] += 1
                        continue
                    result["added"] += 1
                    image = self.image(uploaded["id"])
                    width,height = image["width"],image["height"]
                    split = next((part for part in PurePosixPath(n).parts if part in {"train","val","valid","test"}),"unassigned")
                    if split == "valid":
                        split = "val"
                    objects, status, notes = [], "unannotated", ""
                    if backup:
                        for a in backup["annotations"]:
                            objects.append({**a,"class_id":mapping[incoming_classes[a["class_id"]].casefold()]})
                        status,split,notes = backup["status"],backup["split"],backup.get("notes", "")
                    elif n in coco_by_path:
                        meta, annotations = coco_by_path[n]
                        if meta.get("width") != width or meta.get("height") != height or uploaded["orientation_changed"]:
                            raise Problem("COCO dimensions/orientation differ from imported pixels. Re-export images without EXIF rotation.")
                        for a in annotations:
                            obj = {"id":ident(),"class_id":mapping[a["_class_name"].casefold()]}
                            seg = a.get("segmentation",[])
                            if seg:
                                coords=seg[0]
                                obj.update(type="polygon",points=[coords[i:i+2] for i in range(0,len(coords),2)])
                            else:
                                obj.update(type="box",**dict(zip(("x","y","w","h"),a["bbox"])))
                            objects.append(obj)
                        status = "annotated"
                    elif n in yolo_by_path:
                        if uploaded["orientation_changed"]:
                            raise Problem("YOLO labels need images without EXIF rotation. Re-export oriented pixels first.")
                        for name, coords in yolo_by_path[n]:
                            obj = {"id":ident(),"class_id":mapping[name.casefold()]}
                            if len(coords) == 4:
                                cx,cy,w,h = coords
                                x1,y1,x2,y2 = max(0,(cx-w/2)*width),max(0,(cy-h/2)*height),min(width,(cx+w/2)*width),min(height,(cy+h/2)*height)
                                obj.update(type="box",x=x1,y=y1,w=x2-x1,h=y2-y1)
                            else:
                                obj.update(type="polygon",points=[[coords[i]*width,coords[i+1]*height] for i in range(0,len(coords),2)])
                            objects.append(obj)
                        status="annotated"
                    metadata = sample_fields(backup or {}, {})
                    self.save(image["id"],{"revision":0,"annotations":objects,"status":status,"split":split,"notes":notes,
                                           "confirm_negative":True,**metadata}, area_spec=measurement_spec(backup or {}))
                    if status in {"annotated","reviewed"}:
                        result["labeled"] += 1
                except (Problem, KeyError, ValueError, TypeError) as exc:
                    result["errors"].append({"file":basename(n),"error":str(exc),"image_added":bool(uploaded and not uploaded.get("duplicate"))})
            return result

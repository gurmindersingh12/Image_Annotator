"""Link image evidence to a field sample sheet, with a reviewable atomic update."""
import csv
import hashlib
import io
import json
from collections import defaultdict

from store import Problem, SAMPLE_FIELDS, csv_text, now, record_history, sample_fields


IDENTITY = ("observation_id", "image_id", "image_name")
COLUMNS = (*IDENTITY, "revision", *SAMPLE_FIELDS)
MAX_BYTES = 512 * 1024
MAX_ROWS = 5000


def project_rows(db, project_id):
    if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
        raise Problem("Project not found.", 404)
    return [dict(row) for row in db.execute("""
        SELECT id, observation_id, name, revision, sample_id, cultivar, replicate, collection_date
        FROM images WHERE project_id=? ORDER BY created_at,id
    """, (project_id,))]


def sample_sheet_csv(store, project_id):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    with store.db() as db:
        for image in project_rows(db, project_id):
            writer.writerow([image["observation_id"], image["id"], csv_text(image["name"]), image["revision"],
                             *[csv_text(image[key]) for key in SAMPLE_FIELDS]])
    return out.getvalue().encode("utf-8-sig")


def parse_rows(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise Problem("Choose a UTF-8 CSV with a header and at least one image row.")
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise Problem("Use a sample sheet of up to 512 KB.", 413)
    reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff"), newline=""), strict=True)
    try:
        headers = [value.strip() for value in (reader.fieldnames or [])]
        if "" in headers or len(set(headers)) != len(headers):
            raise Problem("Use a unique name for each column.")
        if not set(IDENTITY).intersection(headers):
            raise Problem("Include image_name, observation_id or image_id to identify each image.")
        if not set(SAMPLE_FIELDS).intersection(headers):
            raise Problem("Include sample_id, cultivar, replicate or collection_date.")
        reader.fieldnames = headers
        rows = []
        for row in reader:
            if None in row or None in row.values():
                raise Problem(f"CSV line {reader.line_num} has a different number of cells from its header.")
            if not any(cell.strip() for cell in row.values()):
                continue
            rows.append((reader.line_num, {key: row[key].strip() for key in COLUMNS if key in row}))
            if len(rows) > MAX_ROWS:
                raise Problem("Use up to 5,000 images per sample sheet.")
    except csv.Error as exc:
        raise Problem(f"Invalid CSV near line {reader.line_num}. {exc}") from exc
    if not rows:
        raise Problem("Add at least one image row to the sheet.")
    return rows


def resolve_image(row, by_id, by_observation, by_name):
    matches = []
    for column, index in (("image_id", by_id), ("observation_id", by_observation)):
        if row.get(column):
            candidate = index.get(row[column])
            if candidate is None:
                raise Problem(f"The {column} does not match an image in this project.")
            matches.append(candidate)
    name = row.get("image_name", "")
    if matches:
        image = matches[0]
        if any(other["id"] != image["id"] for other in matches):
            raise Problem("Image and observation IDs refer to different images.")
        if name and name not in {image["name"], csv_text(image["name"])}:
            raise Problem("The filename does not match the supplied ID.")
        return image
    if not name:
        raise Problem("Add an image identifier or filename.")
    found = by_name.get(name, [])
    if len(found) > 1:
        raise Problem("This filename occurs more than once. Use the ID from the project template.")
    if not found:
        raise Problem("No image in this project has this filename.")
    return found[0]


def plan_updates(images, rows):
    by_id = {image["id"]: image for image in images}
    by_observation = {image["observation_id"]: image for image in images}
    by_name = defaultdict(list)
    for image in images:
        by_name[image["name"]].append(image)
    changes = []
    seen = set()
    for line, row in rows:
        try:
            image = resolve_image(row, by_id, by_observation, by_name)
            if image["id"] in seen:
                raise Problem("This sheet lists the same image more than once.")
            seen.add(image["id"])
            # A blank cell preserves the current value. Excel-safe template text
            # (e.g. an apostrophe before a formula-like ID) also round-trips.
            updates = {key: row[key] for key in SAMPLE_FIELDS
                       if row.get(key) and row[key] != csv_text(image[key])}
            fields = sample_fields(updates, image)
            diffs = {key: [image[key], fields[key]] for key in SAMPLE_FIELDS if fields[key] != image[key]}
            if not diffs:
                continue
            if row.get("revision") and row["revision"] != str(image["revision"]):
                raise Problem("The saved image has changed. Download a fresh template.", 409)
            changes.append({"image_id": image["id"], "image_name": image["name"],
                            "revision": image["revision"], "fields": diffs})
        except Problem as exc:
            raise Problem(f"CSV line {line}: {exc}", exc.status) from exc
    return changes


def process_sample_sheet(store, project_id, payload):
    mode = payload.get("mode")
    if mode not in {"preview", "apply"}:
        raise Problem("Choose preview or apply.")
    rows = parse_rows(payload.get("csv"))
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE" if mode == "apply" else "BEGIN")
        changes = plan_updates(project_rows(db, project_id), rows)
        # Bind approval to the exact live images, their revisions and the
        # proposed values even when the incoming sheet contains only filenames.
        proof = json.dumps([project_id, changes], separators=(",", ":"), sort_keys=True)
        token = hashlib.sha256(proof.encode("utf-8")).hexdigest()
        if mode == "apply":
            if payload.get("token") != token:
                raise Problem("The project or sheet changed after preview. Preview the file again.", 409)
            stamp = now()
            for change in changes:
                previous = dict(db.execute("SELECT * FROM images WHERE id=?",
                                           (change["image_id"],)).fetchone())
                previous["annotations"] = json.loads(previous["annotations"])
                record_history(db, previous, stamp)
                values = {key: previous[key] for key in SAMPLE_FIELDS}
                values.update({key: pair[1] for key, pair in change["fields"].items()})
                db.execute("""UPDATE images
                    SET sample_id=?, cultivar=?, replicate=?, collection_date=?, revision=revision+1, updated_at=?
                    WHERE id=?""", (*[values[key] for key in SAMPLE_FIELDS], stamp, previous["id"]))
    return {"rows": len(rows), "changed": len(changes), "unchanged": len(rows)-len(changes),
            "examples": changes[:8], "token": token, "applied": mode == "apply"}

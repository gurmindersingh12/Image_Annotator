#!/usr/bin/env python3
"""Image Annotator: a single-user, loopback-only local annotation app."""
from __future__ import annotations

import argparse
import csv
import io
import json
import mimetypes
import os
import re
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from store import APP_NAME, APP_SLUG, MAX_ARCHIVE, MAX_UPLOAD, VERSION, Problem, Store

BASE = Path(__file__).resolve().parent


def default_data_dir():
    """Keep the source edition's existing workspace and bundled editions' user data."""
    if not getattr(sys, "frozen", False):
        return BASE / "data"
    if sys.platform == "win32":
        parent = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        parent = Path.home() / "Library" / "Application Support"
    else:
        data_home = Path(os.environ.get("XDG_DATA_HOME", ""))
        parent = data_home if data_home.is_absolute() else Path.home() / ".local" / "share"
    return parent / APP_SLUG / "data"


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_SLUG}/{VERSION}"

    @property
    def store(self):
        return self.server.store

    def log_message(self, fmt, *args):
        if self.path.startswith('/api/') and self.command != 'GET':
            super().log_message(fmt, *args)

    def send(self, body, status=200, content_type="application/json; charset=utf-8", download=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "private, max-age=60")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_body(self, limit=2 * 1024 * 1024):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Problem("Invalid Content-Length.")
        if length < 0 or length > limit:
            raise Problem(f"Request exceeds the {limit // (1024*1024)} MB limit.", 413)
        data = self.rfile.read(length)
        if len(data) != length:
            raise Problem("Upload was interrupted.")
        return data

    def json_body(self):
        try:
            value = json.loads(self.read_body() or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise Problem("Invalid JSON request.")
        if not isinstance(value, dict):
            raise Problem("Request must be a JSON object.")
        return value

    def check_origin(self):
        # Reject browser cross-origin writes, DNS rebinding, and cross-site forms.
        host = self.headers.get("Host", "")
        allowed = self.server.allowed_hosts
        if host not in allowed:
            raise Problem("Unrecognized local host.", 403)
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}" and origin != f"https://{host}":
            raise Problem("Cross-origin requests are disabled.", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise Problem("Cross-site requests are disabled.", 403)
        if self.command in {"POST", "PUT", "PATCH", "DELETE"} and self.headers.get("X-ImageAnnotator") != "1":
            raise Problem("Missing local app request header.", 403)

    def handle_request(self):
        try:
            self.check_origin()
            self.route()
        except Problem as exc:
            self.send({"error":str(exc)}, exc.status)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            self.send({"error":"Invalid request: " + str(exc)}, 400)
        except Exception:
            traceback.print_exc()
            self.send({"error":"The local server encountered an error. See the terminal for details."}, 500)

    do_GET = handle_request
    do_POST = handle_request
    do_PUT = handle_request

    def route(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        method = self.command
        if path == "/api/health" and method == "GET":
            return self.send({"ok":True,"app":APP_NAME,"version":VERSION,"data_directory":str(self.store.root)})
        if path == "/api/projects":
            if method == "GET":
                return self.send(self.store.projects())
            if method == "POST":
                p = self.json_body()
                return self.send(self.store.create_project(p.get("name",""),p.get("description",""),p.get("classes"),p.get("preset","custom")),201)
        if path == "/api/demo" and method == "POST":
            self.read_body()
            return self.send(self.store.demo(),201)
        match = re.fullmatch(r"/api/projects/([a-f0-9]{32})(?:/(images|upload|classes|import|split|export|counts|observations|object-areas|sample-sheet))?(?:/([a-f0-9]{32}))?",path)
        if match:
            project_id, action, class_id = match.groups()
            if not action and method == "GET":
                return self.send(self.store.project(project_id))
            if action == "images" and method == "GET":
                return self.send(self.store.images(project_id))
            if action == "upload" and method == "POST":
                raw = self.read_body(MAX_UPLOAD)
                return self.send(self.store.upload(project_id,unquote(self.headers.get("X-Filename","image.png")),raw),201)
            if action == "classes" and method == "POST":
                return self.send(self.store.add_class(project_id,self.json_body().get("name","")),201)
            if action == "classes" and class_id and method == "PUT":
                data = self.json_body()
                return self.send(self.store.update_class(project_id,class_id,data.get("name",""),data.get("color","")))
            if action == "split" and method == "POST":
                p = self.json_body()
                return self.send(self.store.split(project_id,p.get("train",80),p.get("val",20),p.get("test",0),p.get("seed",42)))
            if action == "import" and method == "POST":
                return self.send(self.store.import_archive(project_id,self.read_body(MAX_ARCHIVE)))
            if action == "export" and method == "GET":
                fmt=query.get("format",["yolo"])[0]
                raw=self.store.export(project_id,fmt,query.get("scope",["completed"])[0],query.get("images",["1"])[0] == "1")
                return self.send(raw,content_type="application/zip",download=f"{APP_SLUG}_{fmt}_{project_id[:8]}.zip")
            if action == "sample-sheet":
                from field_sheet import process_sample_sheet, sample_sheet_csv

                if method == "GET":
                    return self.send(sample_sheet_csv(self.store, project_id), content_type="text/csv; charset=utf-8",
                                     download=f"{APP_SLUG}_sample_sheet.csv")
                if method == "POST":
                    return self.send(process_sample_sheet(self.store, project_id, self.json_body()))
            if action == "observations" and method == "GET":
                body = self.store.observation_csv(project_id, query.get("scope", ["all"])[0])
                return self.send(body, content_type="text/csv; charset=utf-8", download=f"{APP_SLUG}_fieldbook.csv")
            if action == "object-areas" and method == "GET":
                body = self.store.object_area_csv(project_id, query.get("scope", ["all"])[0])
                return self.send(body, content_type="text/csv; charset=utf-8", download=f"{APP_SLUG}_object_areas.csv")
            if action == "counts" and method == "GET":
                p = self.store.project(project_id)
                out=io.StringIO(newline="")
                writer=csv.writer(out)
                def safe(v):
                    v=str(v)
                    return "'"+v if v.lstrip().startswith(("=","+","-","@")) else v
                writer.writerow(["image","status","split","total_objects","notes"]+[safe(c["name"]) for c in p["classes"]])
                for im in self.store.images(project_id):
                    total = im["measured_object_count"]
                    writer.writerow([safe(im["name"]), im["status"], im["split"], total, safe(im["notes"]),
                                     *[im["class_counts"].get(c["id"], 0) if total is not None else None
                                       for c in p["classes"]]])
                return self.send(out.getvalue().encode("utf-8-sig"),content_type="text/csv; charset=utf-8",download=f"{APP_SLUG}_counts.csv")
        match = re.fullmatch(r"/api/images/([a-f0-9]{32})(?:/(file|thumbnail|history|restore|leaf-area))?",path)
        if match:
            image_id, action = match.groups()
            if not action:
                if method == "GET":
                    return self.send(self.store.image(image_id))
                if method == "PUT":
                    return self.send(self.store.save(image_id,self.json_body()))
            if method == "GET" and action in {"file","thumbnail"}:
                im = self.store.image(image_id)
                file = self.store.root / ("images" if action == "file" else "thumbnails") / (im["file"] if action == "file" else image_id+".jpg")
                return self.send(file.read_bytes(),content_type="image/png" if action=="file" else "image/jpeg")
            if method == "PUT" and action == "leaf-area":
                return self.send(self.store.set_leaf_area(image_id, self.json_body()))
            if method == "GET" and action == "history":
                return self.send(self.store.history(image_id))
            if method == "POST" and action == "restore":
                p=self.json_body()
                return self.send(self.store.restore_revision(image_id,int(p["restore_revision"]),int(p["revision"])))
        if method == "GET" and not path.startswith("/api/"):
            target=(BASE / "static" / (path.lstrip("/") or "index.html")).resolve()
            if not target.is_relative_to((BASE/"static").resolve()) or not target.is_file():
                raise Problem("File not found.",404)
            content_type=mimetypes.guess_type(target)[0] or "application/octet-stream"
            return self.send(target.read_bytes(),content_type=content_type + ("; charset=utf-8" if content_type.startswith("text/") or target.suffix==".js" else ""))
        raise Problem("Endpoint not found.",404)


def make_server(host="127.0.0.1", port=8765, data_dir=None, preview_origin=None):
    server = ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads = True
    try:
        server.store = Store(data_dir or default_data_dir())
    except Exception:
        server.server_close()
        raise
    actual_port = server.server_address[1]
    server.allowed_hosts = {f"localhost:{actual_port}",f"127.0.0.1:{actual_port}",f"[::1]:{actual_port}"}
    if preview_origin:
        server.allowed_hosts.add(urlparse(preview_origin).netloc)
    return server


def main():
    parser = argparse.ArgumentParser(description=f"Run {APP_NAME} locally.")
    parser.add_argument("--port",type=int,default=int(os.environ.get("PORT","8765")))
    parser.add_argument("--data-dir",type=Path,help="Open or create a workspace in this folder.")
    parser.add_argument("--no-browser",action="store_true")
    # Preview mode is used only by the development daemon, never by launchers.
    parser.add_argument("--preview",action="store_true",help=argparse.SUPPRESS)
    args=parser.parse_args()
    try:
        server=make_server("0.0.0.0" if args.preview else "127.0.0.1",args.port,args.data_dir,os.environ.get("IMAGEANNOTATOR_PREVIEW_ORIGIN"))
    except OSError as exc:
        raise SystemExit(f"Could not start {APP_NAME}: {exc}. If the port is in use, close the other instance or pass --port 8766.")
    url=f"http://127.0.0.1:{server.server_address[1]}"
    print(f"\n{APP_NAME} {VERSION}\nOpen {url}\nData: {server.store.root}\nKeep this terminal open. Press Ctrl+C to stop.\n",flush=True)
    if not args.no_browser:
        # An external Linux browser needs its original system library path.
        if getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
            if "LD_LIBRARY_PATH_ORIG" in os.environ:
                os.environ["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH_ORIG"]
            else:
                os.environ.pop("LD_LIBRARY_PATH", None)
        threading.Timer(.8,lambda:webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n{APP_NAME} stopped. Your work is saved.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

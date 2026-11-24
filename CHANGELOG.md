# Changelog

## 1.0.2 · Move and resize boxes

- With **B** active, drag from empty space to draw another box, or drag inside a box to move it.
- Resize a box from a corner or an edge handle. Hold **Shift** while dragging to draw a new box over an existing one.

## 1.0.1 · Bounding box selection

- Press **B** or select the box tool to draw a rectangle under the active class, including **Leaf outline** and **Damaged tissue** in the soybean trial. The selected class also applies to boxes accepted with **Find similar**.
- Press **P** to trace tissue for a damage percentage. A box under either measurement class leaves the percentage blank until a polygon replaces it.

## 1.0.0 · Initial public release

- Create local projects, import images, and draw, edit and review boxes or polygons. Use an example box to find similar objects in the same image.
- Set the image scale from two ruler marks and report projected areas in cm². On a leaf, measure visibly green pixels inside a selected region or traced blade.
- Link images to a sample sheet, record seed or nodule counts, and score manually outlined leaf damage. Open a synthetic soybean trial with known measurements to try the workflow.
- Export image observations, per-object areas and YOLO or COCO annotations. Keep repeated views of the same sample together during automatic dataset splitting.
- Run from source on Windows, macOS or Linux. Use the included build workflow to create native desktop packages.

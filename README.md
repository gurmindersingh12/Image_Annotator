# Image Annotator

Draw boxes or polygons on photographs and scans, give each object a class, and export the annotations. If an image includes a ruler, mark a known length once to measure the projected area of each drawn object in cm². A synthetic soybean leaf trial illustrates the workflow. The same tools work with dry bean leaves and other two-dimensional specimens.

The browser edition uses the same editor and runs on the viewer’s device. [Build the website or publish it with GitHub Pages](web/README.md)

## Start on a Windows PC

1. Install **Python 3.10 or newer**. If the installer offers **Add Python to PATH**, select it. [Python for Windows](https://www.python.org/downloads/windows/)
2. Right-click the source ZIP, choose **Extract All**, and open the extracted **ImageAnnotator** folder.
3. Double-click **Start_ImageAnnotator.bat**. The first launch uses the internet to install Pillow and PyYAML. Your browser opens the app on your own computer, normally at **http://127.0.0.1:8765**.
4. Try the practice trial, or create a new project and choose images from your PC. Keep the terminal open while annotating. Press **Ctrl+C** there to stop the app. Use the same launcher to return to your saved projects.

**macOS or Linux source.** Install Python 3.10 or newer, extract the source ZIP and run `python3 launcher.py` in the extracted folder. All source editions run offline after the first setup.

**Linux x64 desktop.** Extract `ImageAnnotator-1.0.2-Linux-x64.tar.gz`, open its ImageAnnotator folder in a terminal and run `./ImageAnnotator`. Python is included. This archive was tested on Debian 13 x64. Build workflows for native Windows and macOS packages are included. [Desktop builds and data locations](docs/desktop.md)

Get a project running and export its first measurements with the [Image Annotator wiki](wiki/Home.md).

## Annotate and measure a soybean leaf

1. Lay the leaf flat on a plain, non-green surface. Put the ruler beside it at the same height and take the photo straight down. A clearly marked scale bar in a scan also works.
2. Create an annotation project and add your image. Choose a class such as **Leaf**. Press **B** or select the box tool, then drag from one corner of the leaf to the opposite corner. With **B** active, drag inside an existing box to move it, or drag a corner or edge handle to resize it. Hold **Shift** while dragging to start a new overlapping box. For a shape-specific area, press **P** and click around the leaf margin. Press **Enter** to finish a polygon.
3. Open **Set scale / measure area**. Mark two ticks, enter their actual separation in mm or cm, and save the scale. Select your annotation to read its area. A box measures its enclosing rectangle, including any background at the corners. A blade traced with a polygon includes green and discolored tissue within the boundary.
4. To measure green tissue, choose **Green leaf** in the scale dialog. Restrict the analysis to the leaf, inspect the highlighted pixels, and save the measurement. The saved green area depends on the color threshold. Classify damage separately if the study calls for it.
5. Review the image. The **Per-object areas** CSV gives one row per annotation. The observations CSV gives one row per source image with its measurements and review state. The sample sheet can link several images to the same experimental unit.

For a **Leaf damage** project, boxes can label or locate soybean leaves and lesions. To calculate damage percentage, trace the blade under **Leaf outline** and affected regions under **Damaged tissue** with **P**. A box in either of those two classes leaves the percentage blank until you replace it with a polygon.

Choose the practice trial on the welcome screen and open **SB-101, view 1**. The ruler spans 2 cm between the 0 and 2 ticks. Its traced blade measures **42.44 cm²**. At the saved threshold, **41.15 cm²** of the blade meets the green-pixel criterion.

You can also use the app for seed counts, root nodules, or custom classes. YOLO and COCO exports carry the labels into a training dataset. Automatic splitting keeps repeated views with the same nonempty sample ID together.

## Research and development

- [User guide](docs/user-guide.md), [calibrated area](docs/calibrated-area.md) and [visible leaf damage](docs/leaf-area.md)
- [Version 1.0.0 software note](paper/software-note.pdf) and [editable manuscript](paper/manuscript.md)
- [Reproducible box-suggestion experiment](evaluation/README.md)
- [Contribution guide](CONTRIBUTING.md), [citation](CITATION.cff) and [MIT software license](LICENSE)
- [Public release checklist](docs/release.md)

Install the Python dependencies from `requirements.txt` and run `python -m unittest discover -s tests -v`. With Node.js 18 or newer, run `node --test` and `node evaluation/run.cjs`. The runtime itself uses Python and a browser.

The field photograph on the welcome screen is by the United Soybean Board and is distributed under CC BY 2.0. [Photograph and license details](static/assets/photo-credit.txt)

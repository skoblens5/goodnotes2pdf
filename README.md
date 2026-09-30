# goodnotes2pdf

NOTE: This github repo was 100% vibe coded so it may have issues. I tested it on my course notes which are primarly handwritten and it seems to capture this correctly. This includes Goodnote pages with backgrounds, embedded images, and imported pdf files / slides with added annotations. Beyond that it likely will miss things. It is much faster and produces smaller files than the native Goodreads conversion and handles the entire subfolder system automatically :)


Convert GoodNotes notebooks (`.goodnotes` files) to PDF on Windows, macOS or Linux, without the GoodNotes app.

The GoodNotes Windows app exports slowly and often fails on large notebooks, because it turns every page into one large image. goodnotes2pdf reads the `.goodnotes` file directly and draws your handwriting as vector lines. The resulting PDFs stay sharp at any zoom, are often smaller than GoodNotes' own export, and convert quickly: a 343-page notebook takes about 30 seconds.

## What it converts

Each page comes out at its GoodNotes size, in the same order as in the notebook, with pages you deleted left out. The page shows its paper template (dot grid, lined, blank, cover and so on) or, for imported documents, the correct page of the imported PDF, such as a lecture slide.

On top of that, the converter draws everything you added in GoodNotes:

- **Ink:** pen strokes with their stored colour and thickness, and highlighter strokes, which are semi-transparent and sit underneath the ink.
- **Shapes:** lines, polygons, boxes, ovals and curves made with the shape tool, including filled shapes.
- **Moved ink:** anything you moved with the lasso tool appears where you moved it.
- **Images:** pasted or inserted images, placed at their size and rotation, with GoodNotes' thin frame and stacked in GoodNotes' order.

It also carries over two kinds of bookmarks. GoodNotes outline entries (for example "HW 1" on page 31) become PDF bookmarks. The outline that came inside an imported PDF, such as a lecture deck's slide titles, is added under a separate "Imported PDF outline" entry.

## Installation

You need Python 3.9 or newer and the PyMuPDF library.

1. Install Python from [python.org](https://www.python.org/downloads/). On Windows, tick **"Add Python to PATH"** during installation.
2. Open a Command Prompt (or terminal) and install PyMuPDF:

   ```
   pip install pymupdf
   ```

3. Put `goodnotes2pdf.py` in any folder.

## Quick start

To convert every notebook in a folder, including all its subfolders, run:

```
python goodnotes2pdf.py convert "C:\Notes" "C:\NotesPDF"
```

The PDFs are written to `C:\NotesPDF`, mirroring the folder structure of `C:\Notes`. Notebooks that already have a PDF are skipped, so you can stop and restart at any time. Add `--overwrite` to convert everything again, for example after updating the script.

To convert a single notebook:

```
python goodnotes2pdf.py convert "C:\Notes\CFD.goodnotes" "C:\NotesPDF"
```

If you prefer not to use the command line, double-click `goodnotes2pdf.py` or run it with no arguments. Two folder pickers appear, one for your notebooks and one for the PDFs.

## Options for `convert`

| Option | Effect |
|---|---|
| `--overwrite` | Re-convert notebooks even if their PDF already exists. |
| `--workers N` | Number of notebooks converted in parallel. Defaults to your CPU cores minus one. Use `--workers 1` if memory is tight. |
| `--no-pdf-outlines` | Leave out the outline that came inside imported PDFs, and keep only your own GoodNotes bookmarks. |
| `--pen-width W` | Draw every stroke W points wide instead of using each stroke's stored thickness. |
| `--scale S` | Conversion from GoodNotes units to PDF points. The default, 72/132, is correct for all notebooks tested, so you shouldn't need this. |
| `--mode` | `auto` (default) or `ink` draw the notebook normally. `thumbnails` builds the PDF from the page previews stored in the file, if any. It's a low-resolution last resort. |

## Checking results and troubleshooting

When a batch finishes, the console lists each notebook with its page and stroke counts. Files that failed are listed in `conversion_errors.log` in the output folder. A notebook that converts but can't determine its page order prints a warning in the console.

Three diagnostic commands help when a notebook doesn't look right. None of them print your handwriting or typed text.

```
python goodnotes2pdf.py layout "C:\Notes\notebook.goodnotes"
```

This lists the page order, each page's paper template, the attachments, and the bookmarks with their page numbers. It's the first thing to run if pages are out of order or missing their background.

```
python goodnotes2pdf.py inspect "C:\Notes\notebook.goodnotes" --report report.txt
python goodnotes2pdf.py dump "C:\Notes\notebook.goodnotes" --page media --full --report dump.txt
```

These write lower-level reports on the file's internal structure. `--page` picks which page to dump: a number (counting from 0, in the order pages were created), a comma-separated list, or `media` to pick pages that use imported PDFs or images automatically.

When reporting a problem, the most useful thing to send is the `.goodnotes` file together with GoodNotes' own export of it, or a screenshot of the page that looks wrong.

## Known differences from GoodNotes' export

Colours can look slightly stronger than in GoodNotes' export. The converter uses each stroke's stored colour exactly, while GoodNotes blends thin lines into the white paper when it turns pages into images.

The converter draws lines with a constant width, the thickness stored with each stroke. Strokes that GoodNotes stores as filled outlines come out exactly as drawn.

Images present in the file but not placed on any page are left out, as GoodNotes does. They're usually images you added and later removed or undid.

## Not yet supported

The following haven't appeared in any notebook tested so far, so the converter doesn't handle them yet:

- typed text boxes
- stickers
- web links
- audio recordings
- nested (multi-level) GoodNotes outlines

If a notebook uses any of these, the rest of the page still converts. Only that element is missing.

## How it works

A `.goodnotes` file is a ZIP archive. Pages live in `notes/` as streams of Protocol Buffers records: pen strokes, shapes, fills and images. Stroke geometry inside these records is stored compressed with Apple's LZ4 format. The notebook's structure, meaning page order, paper templates, page moves, deletions and outlines, is recorded as a log of events in `index.events.pb`. Paper templates, imported PDFs and images are stored in `attachments/`, and `index.attachments.pb` maps attachment IDs to file names. Coordinates are in 1/132-inch units, which the converter scales to PDF points (1/72 inch).

None of this is publicly documented. The format was worked out by comparing real notebooks against GoodNotes' own exports, so a future GoodNotes update could change it.
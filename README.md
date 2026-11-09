# Homework grader

A local web app. Download a submission zip and a gradebook CSV from Canvas. The app reads the handwritten pages, drafts a rubric, and after you edit the scores it exports a CSV you can upload again. It does not use the Canvas API.

## Install and start

[uv](https://docs.astral.sh/uv/) needs to be installed.

```powershell
uv sync
uv run canvas-grader
```

Open http://127.0.0.1:8765 in a browser.

## Settings

The settings page has two connections:

- **Online**: any OpenAI-compatible vision API. Fill in the address, model name, and API key. The defaults are DeepSeek at `https://api.deepseek.com` with `deepseek-flash`. The model has to accept images.
- **Local**: LM Studio. Load a vision model, open Developer, and click Start server. Set the address to `http://127.0.0.1:1234/v1`. The model name is the id the server lists. The key can be left blank.

"Test connection" asks that server for its model list, then sends a small image the program draws itself. Student work is never used for the test. A failed vision check usually means the model cannot see images, or the server is not running.

Images are sent in the user message as `image_url`, with content `data:image/jpeg;base64,...`.

## What you do in Canvas

1. Grades → the assignment column → Download Submissions. That is the zip.
2. Grades → Export, and download the gradebook CSV. Leave the assignment column title unchanged (it usually includes the assignment id).
3. Grade and review in this app, then download the new CSV.
4. Grades → Import, and upload that CSV.

The CSV **can only change scores**. Comments and submission status are not included. Comments are saved as a separate zip, one Markdown file per student, so they are easy to paste into SpeedGrader.

Export changes only the column you chose. Other columns and the Points Possible row stay as they were. Students who cannot be matched to the gradebook are not written in.

## How grading works

Each batch can use online or local, and the next class can use a different choice.

1. Upload the zip, the gradebook, and a model answer. Choose how many papers draft the rubric, from 1 up to the whole class. The default is 5, or everyone when the class is smaller.
2. The chosen model reads those papers, then drafts a rubric. When they are sent online they are labeled A, B, C, and so on, with no name, Canvas id, or filename. The gradebook itself is never sent to the model.
3. Compare the original pages with the draft. To discuss it, copy the name-free summary on the page. You cannot grade the class or export scores before you confirm.
4. When you confirm the rubric, choose local or online for the whole class. Local is the default. If you choose online, every student's page images are sent to the online model you configured, again without names.
5. While reviewing you can change scores and comments. Unreviewed students are listed first. Then export.

The default is online for the rubric and local for the class. If you choose local but have not filled in a model name, you cannot start.

Handwritten pages are converted to JPEG, and each page is split into two overlapping halves, top and bottom. More than 12 pages is marked, and only the first 12 are sent. One student is graded at a time.

How well local handwriting recognition works depends on the model you connect. Review is not optional.

## Where data is stored

Submissions, the database, and the API key are in `data/` in the working directory. That folder is in `.gitignore`. Do not commit `data/` or `.env`.

## Tests

```powershell
uv run pytest
```

Tests check filename parsing, gradebook columns, PDF page conversion, and that an upload lists students. They do not call a model.

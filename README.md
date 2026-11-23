# FAR RAG System
A retrieval pipeline over the Federal Acquisition Regulation

## Parsing the FAR

Source: [acquisition.gov/browse/index/far](https://www.acquisition.gov/browse/index/far), FAC 2026-01, effective 03/13/2026.

### Steps

1. Download "Full FAR" as HTML (`FARHTML.zip`) and as Word (`FAR.docx`).
2. Unzip the HTML, which creates `dita_html/`:
   ```bash
   unzip -q FARHTML.zip
   ```
3. The HTML zip ships an empty `Graphics/` folder, so copy the nine figures from the Word file:
   ```bash
   unzip -j -o -q FAR.docx 'word/media/Graphics/*' -d dita_html/Graphics
   ```
4. Parse:
   ```bash
   uv run parse_far.py --input dita_html --output data/parsed
   ```

### Output

| File | Contents |
| --- | --- |
| `data/parsed/sections.jsonl` | One record per FAR section: metadata, full text, and ordered blocks |
| `data/parsed/hierarchy.json` | Subchapter > part > subpart > section tree |
| `data/parsed/parse_report.json` | Counts, integrity checks, warnings, size statistics |

The parser does not chunk. Each section keeps its paragraphs, definitions, tables and figures as separate blocks with their depth and paragraph path (for example `(a)(1)(ii)`), so any chunking strategy can be applied later without re-parsing.

### Results

| Measure | Value |
| --- | --- |
| HTML files read | 3,893 |
| Sections | 3,496 (3,114 with text, 382 empty or reserved) |
| Hierarchy | 8 subchapters, 53 parts, 319 subparts |
| Clauses / provisions (part 52) | 468 / 142 |
| Clause alternates | 204 |
| Blocks | 31,420 |
| Definitions | 1,485 in 269 sections |
| Tables / figures | 88 / 9 |
| Cross-references | 6,351, all resolved |
| Total words | 930,528 |

Words per unit (sections with text):

| Unit | Count | Median | 90th pct | 99th pct | Max |
| --- | --- | --- | --- | --- | --- |
| Section | 3,114 | 152 | 646 | 2,326 | 15,120 |
| Top-level paragraph with its sub-paragraphs | 14,059 | 38 | 141 | 482 | 3,916 |
| Single block | 31,420 | 21 | 65 | 139 | 3,606 |

The largest section is `2.101` (Definitions). The four blocks over 500 words are all tables; the largest is Table 15-2 in `15.408`.

### Integrity checks
- No text lost or duplicated: every character of every section body lands in exactly one block.
- Every section links to a parent, and every cross-reference points to a known section, subpart or part.
- All nine figures have an image file and a transcription.

## Notes on the source data
- **Figures.** The nine images (five formulas, four numbering illustrations) are transcribed by hand in `figure_text.json`. Figure blocks are flagged `transcribed: true` so they can be told apart from verbatim regulation text.
- **Misnamed file.** `42.200.html` in the download contains section 40.200. The parser takes section numbers from the page heading, not the filename.
- **Clause or provision.** The HTML class is unreliable, so the parser decides from the text: the "insert the following ..." sentence first, then the "(End of ...)" line. In 15 entries those two disagree in the FAR itself; they are listed in the report under `clause_kind_conflicts`.
- **List-level warnings.** 18 paragraphs in three entries (`52.230-7`, `52.232-38`, `52.247-64`) have list levels that do not chain in the source HTML. The text is intact; they are listed under `warnings` in the report.
- **Whitespace.** Text is verbatim apart from whitespace: runs are collapsed, and stray spaces around punctuation in the HTML (`3.202 , insert`) are removed.
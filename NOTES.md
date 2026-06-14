# Notes

A running list of things to do, decisions, and open questions.

## Assumptions
- add the backup strategy of ocr where api's are not callable to my assumptions note
- job to be done is not utimate label compliance but comparison between application form and label for match.
- Assumed a human reviewer makes the final determination and corrects the tool's output — the tool is decision-support, not an autonomous approver. Flagged/yellow verdicts (Assumed, Partial match, needs-review) surface nuance for the reviewer to confirm or override rather than auto-approving or rejecting.

## Decisions (latest iteration)
- **Missing vs Mismatch**: split the red "failed" verdict — `✗ Missing` (required value absent from the label) vs `✗ Mismatch` (label has a *different* value). `⚠ Not found` is now yellow and only means "blank on the form AND absent on the label" (nothing to verify).
- **"Found on label" shows the exact matched label text** (the substring the field's pattern hit), not a normalized/extracted value. The extraction machinery still runs for the verdict — it's just no longer the display.
- **Gemini cache persists to a temp dir** (`<tmp>/ttb_gemini_cache`, override `GEMINI_CACHE_DIR`), keyed by image SHA-256, and is **wiped at program start**. This is a deliberate, startup-cleared exception to the "nothing persists" stance — it stores label *readings* (text), not uploaded images, and only when the Gemini backend is used.
- **OCR-digit rescue for net contents** (`75O ML`→`750`) only confirms a value that equals an expected one — it can rescue a faint/garbled volume but never invents a match.
- **Per-item designator is the `ttb_id`** from the application data, not the image filename (they can differ).

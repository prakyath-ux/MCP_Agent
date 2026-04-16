# Global Test Files

Drop 5 dummy files here — the agent will pick one based on each upload
field's `accept` attribute during explore and execute.

## Required files (exact names)

| File | Purpose | Size hint | Used when accept contains |
|---|---|---|---|
| `dummy.jpg` | Photos, IDs, signatures | ~50 KB, any image | `image/*`, `.jpg`, `.jpeg` |
| `dummy.png` | Icons, transparent art, signatures | ~10 KB, transparent | `.png`, `image/*` |
| `dummy.pdf` | Statements, contracts, certificates | ~20 KB, 1 page lorem ipsum | `.pdf`, `application/pdf` |
| `dummy.docx` | Resumes, letters | ~15 KB, "Test Document" boilerplate | `.docx`, `.doc`, Word MIME |
| `dummy.csv` | Bulk data imports | ~1 KB, 3 rows | `.csv`, `text/csv` |

## How selection works

1. Per-app override wins: `artifacts/test_files/{app_name}/registry.json`
   maps `element_id → path`.
2. Per-app semantic match: file in `artifacts/test_files/{app_name}/`
   whose name matches the element's semantic_hint (e.g. `profile.jpg`).
3. Global semantic default: hint → generic file here (e.g. `profile_picture`
   → `dummy.jpg`).
4. Fallback by accept attribute: `image/*` → `dummy.jpg`, else `dummy.pdf`.

## What if the app rejects a dummy?

Some apps do content validation — passport numbers, real-looking IDs, etc.
When that happens, drop a real-looking file into
`artifacts/test_files/{app_name}/` with the matching semantic name, or add
a registry.json entry. No code changes needed.

# Google Drive Backup (D.7)

Autopilot stays an **offline-first** renderer. The Drive upload engine
(`modules/drive_upload_engine.py`) is an optional final pipeline stage
that copies each finished MP4 to a Google Drive folder **only when you
explicitly turn it on**. If it is disabled, offline, or misconfigured,
the render still completes — the stage reports `skipped` (or a warning)
instead of failing your video.

## What it does

- Runs as pipeline stage 18 (`drive_upload`), right after thumbnails.
- Uploads the final rendered MP4 with a **resumable** session: if WiFi
  dies mid-upload (or you close the app), the confirmed byte count is
  saved to `cache/drive_upload_state/` and the upload continues from
  exactly that offset later — never from byte zero.
- Resumes interrupted uploads automatically before the next render
  (`resume_pending_on_run`) and from **Settings → Google Drive backup →
  Resume pending uploads**.
- Uses the minimal OAuth scope `drive.file`: Autopilot can only see
  files it created itself — never the rest of your Drive.

## One-time setup (about 10 minutes)

1. Create (or reuse) a Google Cloud project → enable the **Google
   Drive API**.
2. Create a **service account** and download its JSON key. Put the
   file at `config/drive_service_account.json` (or set
   `credentials_file` to wherever you keep it). **Keep this file
   private — it is a password.**
3. In Google Drive, create a folder (e.g. `Autopilot Renders`), open
   **Share**, and share it with the service account's `client_email`
   (Editor). Copy the folder ID from its URL
   (`drive.google.com/drive/folders/<THIS_PART>`).
4. Edit `config/drive_upload.json`:

```json
{
  "enabled": true,
  "credentials_file": "config/drive_service_account.json",
  "folder_id": "<THIS_PART>",
  "chunk_size_mb": 8
}
```

5. Render anything. The final stage logs the upload; the Drive file
   appears in your folder with the same file name as the MP4.

## Config reference (`config/drive_upload.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Master switch. Nothing uploads while false. |
| `credentials_file` | `config/drive_service_account.json` | Service-account JSON path (absolute or repo-relative). |
| `folder_id` | `""` | Target Drive folder. Empty = service account's own root. |
| `chunk_size_mb` | `8` | Upload chunk size (0.25 MiB quantum, clamped 0.25–64). |
| `resume_pending_on_run` | `true` | Resume old sessions before each new upload. |
| `connect_timeout_seconds` / `read_timeout_seconds` | `10` / `120` | Network timeouts. |
| `drive_endpoints` / `mime_types` | `{}` | Advanced overrides (endpoints exist for offline tests). |

## Honest failure behaviour

| Situation | Render result | What happens |
| --- | --- | --- |
| Disabled / not configured | completes | stage `skipped`, reason logged |
| No internet at start | completes | warning; nothing pending (no session began) |
| Drop mid-upload | completes | warning; session persisted → resumes later |
| Bad credentials / API rejects | completes | warning with the API's error text |
| Session expired (404) | completes | one automatic fresh-session restart |

State files live in `cache/drive_upload_state/*.upload.json` and are
deleted once their file lands safely. Deleting that folder only loses
resume positions — never the rendered MP4s.

## Frozen exe notes

`requests` + `cryptography` are already bundled by
`build/autopilot.spec`; no extra install is needed. Put the
service-account JSON beside the exe under
`_internal/config/drive_service_account.json` (or use an absolute
`credentials_file` path).

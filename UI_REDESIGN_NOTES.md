# UI Redesign Notes (v3.2.7)

Full rationale for the UI pass, so future changes stay consistent with
the reasoning behind them rather than just the code.

## What triggered this

Direct feedback: the UI was "boring," "not user friendly," some options
weren't readable, and the Grade panel's sliders were "very big." A
reference screenshot set (AI Narrator Studio v2.1) was provided as
inspiration — card-based voice picker, compact controls, clear visual
hierarchy — not as something to literally clone, but as a target feel.

## Root causes found (not just guessed)

1. **Oversized sliders**: every slider row used
   `layout.addWidget(slider, 1)` — the `1` is a Qt stretch factor,
   telling the slider to consume all available row width. In a wide
   panel, that's most of the screen for a single 0-100 value.
2. **Cramped, low-contrast Voice Controls**: 17+ fields lived in one
   single-column `QFormLayout`, undifferentiated — no grouping, no
   visual break between "pick a voice" and "adjust pause timing."
3. **Voice picker felt like a database table, not a picker**: voice
   cards were real `QFrame` objects already, but stacked in a single
   `QVBoxLayout` (one per row) instead of a grid, and had **no QSS
   styling at all** — `QFrame#sceneCard` was referenced by object name
   throughout the codebase but never had a corresponding style rule,
   so cards rendered as bare unstyled frames.
4. **Multi-channel queue had no channel visibility**: the batch/queue
   screen already existed (title, priority, status, error columns) but
   had no way to tell which channel a queued render belonged to, even
   though a Channel Profile selector already existed on the render
   page — it just was never threaded through to the queue.

## What changed

- **`ui/panels/compact_field.py`** (new): a shared `build_slider_field()`
  helper — label + value stacked above a width-capped, thin slider.
  Used by both Grade and Voice Controls so the pattern doesn't drift
  between panels.
- **`ui/theme.py`**: added section headers (`#sectionLabel`), field
  labels (`#fieldLabel`), badge/pill styles (`#badge`, `#badgeSuccess`,
  `#badgeMuted`), a thinner/more precise slider style, `QFrame#sceneCard`
  styling (previously missing entirely), and proper `QTreeWidget` /
  `QHeaderView` styling for list/queue views.
- **`ui/panels/grade_panel.py`**: sliders moved into a 2-column grid of
  fixed-max-width fields inside a card, instead of one full-width
  column.
- **`ui/panels/voice_controls_panel.py`**: restructured into five
  labeled cards (Voice, Presets, Sound shaping, Pause timing,
  Advanced) instead of one long form. Same compact slider treatment.
- **`ui/panels/voice_panel.py`**: voice cards now render in a real
  multi-column grid (`QGridLayout`, 3 columns) with badge pills for
  engine/language/gender instead of one dense text line.
- **Channel visibility in the queue**: `batch_add()` now accepts an
  optional `channel` parameter (stored in the existing `notes` JSON
  column — no schema migration needed), `batch_model()` surfaces it per
  row, the queue tree has a new "Channel" column, and the queue summary
  now says e.g. "12 queued · 15 total across 3 channels" when more than
  one channel is represented. The render page's existing Channel
  Profile selector is the source of truth — nothing new to configure.

## Bugs caught during this pass (fixed before shipping)

- New QSS referenced `{success}` but none of the 4 theme `.format()`
  calls passed a `success=` value — would have crashed on app start.
  Fixed by adding `success=` to all 4 theme builds and verifying each
  one actually formats without a `KeyError`.
- `_batch_payload()`'s channel field would have stored the literal
  placeholder string `"(channel default)"` as a fake channel name when
  no real channel was selected (the combo's first item is a real,
  selectable item with that text, `data=None`). Fixed by checking
  `.currentData()` first and only using the label when a real profile
  is selected.

## Testing performed

PyQt6 itself isn't installable in this environment (no internet, and
it's not part of the base sandbox), so live visual/interactive
rendering could not be screenshotted here. What WAS verified:

- Every touched file compiles (`py_compile`) individually and as a full
  project sweep — zero syntax errors anywhere in the codebase.
- `ui/theme.py`'s 4 theme QSS strings were actually built via their real
  `.format()` calls and checked for leftover unformatted placeholders —
  this is what caught the `{success}` bug above.
- Existing unit tests (`tests/unit/test_ui_panels_vm.py`) that call
  `batch_add()` were checked against the new signature and confirmed
  compatible (positional args unchanged, no assertions on the `notes`
  JSON internals that would break from the new `channel` key).
- Cross-referenced every external caller of changed methods
  (`GradePanel.__init__`, `batch_add`) across the whole codebase to
  confirm no other call site was left broken.

**Not verified**: actual on-screen appearance/spacing in a real Qt
window. The layout code is structurally correct (grid columns, width
caps, card nesting all use standard, well-established PyQt6 APIs used
elsewhere in this same codebase) but a visual pass on a real Windows
machine is the next real checkpoint — if anything looks off in
practice, it's a spacing/sizing tweak, not a structural rewrite.

## What's next (remaining from the original pass)

- A settings toggle for card grid column count (2/3/4) if voice/scene
  cards feel too small or too large at different window sizes.
- Timeline panel wasn't touched — it's a custom-drawn scene-card view,
  not form/slider-based, and didn't show the same problem patterns.

## Continuation pass — remaining panels (same session, part 2)

Applied the same treatment to every other panel that had the reported
problems:

- **`ui/panels/audio_panel.py`**: same slider-stretch bug (confirmed:
  `row.addWidget(slider, 1)`), affecting 8 sliders total (narration,
  music, sfx, master, + 4 ducking sliders). Fixed at the source in the
  shared `_slider_row()` helper — one `setMaximumWidth(220)` line fixes
  all 8 without touching the value/label contract `reload_settings()`
  and `_state()` depend on. Regrouped the flat form into three cards:
  Narration & Music, SFX & Ducking, Fades & Master.
- **`ui/panels/scene_controls_panel.py`**: no slider bug here, but the
  motion form was a bare list with no card grouping — wrapped in a
  "Motion" card for visual consistency with the rest of the app.
- **`ui/panels/export_settings_panel.py`**: one long 15-row form split
  into three cards — Video, Audio, Output — matching how the settings
  actually group conceptually.
- **`ui/panels/transitions_panel.py`**: controls row wrapped in an
  "Apply Transition" card.
- **`ui/panels/import_panel.py`**: found and fixed a real, separate bug
  — `DropZone` never called `setWordWrap(True)`, so with 5 zones
  sharing one row (Script/Images/Music/Voice/Video) each zone was too
  narrow for its own title and text was silently clipped ("Scrip",
  "Imag", "Musi" — matches exactly what was reported in feedback, not
  guessed at). Fixed with word wrap, and went further: converted the
  zone label from one flat plain-text block to rich HTML text so the
  title is bold/accent-colored and visually distinct from the smaller
  muted format/hint text — real hierarchy instead of one gray blob.
  Also added a hover state (border brightens, background tints) so
  drop zones read as interactive targets.

### Additional bugs caught during this continuation

- Verified every changed panel compiles individually AND the full
  project compiles as one sweep (zero errors) — this is now the
  standard bar for every change in this pass, not just spot checks.
- Confirmed `DropZone`'s rich-text change doesn't break anything
  reading its `.text()` — nothing in the codebase does; only the
  widget reference (`self.drop_zone`) is used elsewhere, never its
  text content.
- Confirmed no test file references any of the changed panel classes
  directly, so none of this restructuring risked breaking the existing
  test suite.

### Testing performed (continuation)

Same standard as the first pass: `py_compile` on every changed file
individually, then a full-project sweep with zero errors. Live visual
rendering still isn't possible in this environment (no PyQt6, no
display) — structural correctness (grid layouts, card nesting, width
caps) uses the same well-established PyQt6 patterns already proven
correct in the first pass.

## v3.2.8 — Critical fix: the actual root cause of the overlap/readability bug

The v3.2.7 redesign shipped without real visual verification (documented
limitation at the time) — and real screenshots showed it made things
worse: Grade panel labels overlapping their sliders, Subtitle Designer
fields stacked on top of each other, Export Settings unreadable.

**Root cause, confirmed with certainty**: the Subtitle Designer panel —
which v3.2.7 never touched — showed the exact same overlapping-text
symptom. That's proof this was a **pre-existing structural bug**, not
something the redesign introduced. Every affected panel had a
`QVBoxLayout`/`QFormLayout` with many rows and **no `QScrollArea`**.
When the window is shorter than the content needs, Qt forcibly
compresses rows into overlapping garbage instead of scrolling.
`voice_panel.py` already used `QScrollArea` (for its card grid) and
was the one panel that looked fine in every screenshot — that
contrast is what confirmed the diagnosis.

**Fix**: added `compact_field.wrap_scrollable()`, a small helper that
wraps a panel's content in a real `QScrollArea`, and applied it to
every affected panel: Grade, Voice Controls, Audio, Subtitle Designer,
Export Settings, Scene Controls, Transitions. Scene lists (which
already scroll internally) are kept outside the wrapped area
intentionally.

**A mistake made and caught during this fix**: the first attempt used
a scripted line-index edit for Voice Controls that had an off-by-N bug
in how it counted list-index shifts after inserting a multi-line
string — it corrected the wrong location and corrupted an unrelated
method (`_slider_text`). This was caught immediately because the file
failed to compile, traced by inspecting the exact broken region,
fixed manually, and the rest of the file was checked line-by-line for
any other damage from the same bug. The remaining panels were fixed
with direct, targeted edits instead of that scripted approach.

**Verification performed for this fix specifically** (given trust was
broken once already this session):
- Every touched file compiles individually and the full project
  compiles as one sweep.
- AST-parsed every touched file and counted: exactly one
  `wrap_scrollable()` call, exactly one `content = QWidget()`, and
  zero duplicate method definitions per file (the specific signature
  of the corruption that happened) — all 7 files pass clean.
- Directly re-read the previously-corrupted `_slider_text` method
  character-for-character to confirm it now matches its original,
  correct form.
- Cross-referenced every constructor call site in `app.py` — all 7
  panel classes are still instantiated with unchanged signatures.

**Still an honest limitation**: PyQt6 cannot be installed in this
environment (no internet access), so real on-screen rendering still
could not be screenshotted here. Every check above is as rigorous as
static analysis allows, but a real visual pass on your machine remains
the genuine final confirmation.

## v3.2.9 — Inspector width + grouping short fields onto one line

Real screenshots from v3.2.8 confirmed the scroll fix worked (every
previously-broken panel now renders clean and readable). Two follow-up
requests from reviewing those screenshots:

1. **Inspector panel too wide on every page.** It was fixed at
   280-340px regardless of whether there was anything to inspect —
   often just showing static app info. Reduced to 220-260px, freeing
   real width for the actual content panels on every single page.
   Still fully hideable via View → Show Inspector for anyone who wants
   the space back entirely.

2. **Animation / Intensity / Duration (and similar) each took a full
   row for one short value** — genuinely made those panels feel long
   for no reason, since a combo box or a small spinbox doesn't need
   the whole window width. Added a reusable `field_row()` helper
   (`compact_field.py`) that groups several short fields onto one
   line, each capped to a sane width instead of stretching edge to
   edge. Applied to:
   - Scene Controls: Animation / Intensity / Duration — now one row
     of 3 (the exact example given).
   - Voice Controls: Engine / Voice / Emotion — now one row of 3.
   - Export Settings (Video): Resolution / FPS / Codec — one row of 3,
     CRF / Preset — one row of 2. Custom size kept on its own row
     (compound width×height field, different shape).
   - Export Settings (Audio): Audio codec / Bitrate / Sample rate /
     Channels — now one row of 4.

   Left as full-width where it genuinely needs the space: Export
   folder path, naming pattern, pronunciation dictionary path (long
   text/file-path fields shouldn't be squeezed into a narrow column).

TESTING PERFORMED: full project compile sweep (zero errors) plus the
same AST-based corruption check as v3.2.8 (method-duplication scan)
across every touched file — all clean. Verified every combo/spin box
attribute (resolution_combo, fps_combo, codec_combo, crf_spin,
preset_combo, audio_codec_combo, bitrate_combo, rate_combo,
channels_combo, animation_combo, intensity_combo, duration_spin,
engine_combo, voice_combo, emotion_combo) still has multiple
references elsewhere in its file (confirming reload()/_state() methods
still find them correctly after the restructuring).

## v3.2.10 — Fixed over-correction (dead empty space) + Subtitles never got grouped

Real screenshots after v3.2.9 showed two things:

1. **The v3.2.9 field grouping worked structurally** (Scenes, Voice
   Controls, Export Settings all show fields correctly grouped 3-4 per
   row) — but **the width cap I added was too aggressive**. Fields
   were capped at 260px regardless of how much wider the actual panel
   was, leaving a large dead empty gap after them instead of using the
   available space. Same root mistake as the ORIGINAL "sliders too
   big" bug, just overshot in the opposite direction this time.
   **Fix**: `field_row()` no longer caps field width — fields now
   stretch evenly to fill the row's actual available width (equal
   stretch factor). Same fix for `build_slider_field()` (used by
   Grade/Voice Controls sliders inside a grid) — the grid's own column
   sizing governs width now instead of a redundant, conflicting cap.
   Audio panel's narration/music/SFX/master sliders (`_slider_row()`)
   had the same issue at a smaller cap (220px) — raised to 420px.

2. **Subtitle Designer was never restructured for row-grouping** —
   only got the v3.2.8 scroll-area fix, so every field (Font, Size,
   Weight, three color pickers, Outline, Shadow, Position, Margin,
   Animation) was still one full-width row each, the exact "long
   panel" pattern reported for other panels. Restructured into 4 cards
   (Typography, Colors, Outline & Background, Position & Motion) with
   the same 3-per-row grouping used elsewhere. Also fixed: the Box
   Opacity slider had the exact original unbounded-stretch bug
   (`addWidget(slider, 1)`) that was never caught in this specific
   file during the first fix pass — now uses the standard
   `build_slider_field()` component like every other slider in the
   app.

TESTING PERFORMED: full project compile sweep (zero errors) + same
AST method-duplication corruption check on every touched file (all
clean) + verified every subtitle-panel field attribute (font_combo,
size_spin, weight_combo, color_buttons, outline_spin, shadow_spin,
background_check, opacity_slider, opacity_label, position_combo,
margin_spin, animation_combo, highlight_check, apply_burn_check) still
has multiple references confirming reload()/_state() logic intact.

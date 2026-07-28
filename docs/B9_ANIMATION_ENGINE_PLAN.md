# B.9 Plan — `animation_engine` (✅ IMPLEMENTED 2026-07-16)

**Status:** Complete — `modules/animation_engine.py` + real `config/animation_presets.json`
+ 18 unit tests (192/4 green on Linux). Awaiting Windows verification. OpenCV focus
region deferred to DEBT-B9a as planned.
**Sources read:** File 07 `modules_specification.txt` (MODULE 10), File 11 `presets_and_configs.txt` (animation_presets section), current `config/animation_presets.json` (still PLACEHOLDER_PHASE_B).

---

## 1. Purpose

Generate **FFmpeg `zoompan` filter strings** (and related helpers) so still images become animated video segments: Ken Burns, zooms, pans, static hold, etc.

- Optional module (`can be disabled`)
- Used later by **export_engine** when rendering each scene
- Should respect **intensity** (subtle / medium / dramatic) and **mood** from keyword_analyzer

---

## 2. Planned public API

| Method | Role |
|--------|------|
| `get_zoompan_filter(animation_type, duration_seconds, fps, intensity, width=1920, height=1080)` | Build full `zoompan=...` string |
| `select_random_documentary_animation()` | Weighted random from documentary weights |
| `get_animation_for_keyword_mood(mood)` | Mood → animation id |
| `get_available_animations()` | Catalog for UI / validation |
| `validate_animation_settings(type, intensity, duration)` | Clamp / default / warnings |
| `generate_batch_filters(timeline, default_intensity)` | One filter per timeline scene |
| `apply_easing(t, mode)` | linear / ease_in_out / ease_in / ease_out helpers (for expressions) |

Optional stretch (only if time allows in B.9):

| Method | Role |
|--------|------|
| `detect_focus_region(image_path)` | OpenCV/content-aware center for smarter Ken Burns (spec mentions OpenCV later) — **may defer** if OpenCV not in minimal Windows env |

**Recommendation:** Ship B.9 **without** OpenCV first (center-based pan/zoom only). Add smart focus as DEBT or B.9.1 if needed.

---

## 3. Animation types to implement

### From File 07 core presets (required minimum)

| id | Behavior |
|----|----------|
| `slow_zoom_in` | 1.0 → 1.15 center |
| `slow_zoom_out` | 1.15 → 1.0 center |
| `pan_left` | zoom 1.1, pan right→left |
| `pan_right` | zoom 1.1, pan left→right |
| `pan_up` | vertical pan |
| `pan_down` | vertical pan |
| `ken_burns` | zoom 1.0→1.2 + diagonal pan |
| `static` | no motion |
| `diagonal_pan_tl_br` | fixed zoom diagonal |
| `dramatic_zoom_in` | 1.0 → 1.4 |

### From File 11 extended list (include in config + engine)

| id | Notes |
|----|--------|
| `pull_back` | 1.35 → 1.0 |
| `vertical_scan` | document-style top→bottom |
| `drift_float` | subtle drift (may simplify random) |

**Target catalog size:** **~13 animations** (all above).

### Intensity multipliers (File 11)

| Intensity | Scale on zoom delta |
|-----------|---------------------|
| `subtle` | ×0.50 |
| `medium` | ×1.00 |
| `dramatic` | ×1.50 |

### Documentary weights (random pick)

| Animation | Weight |
|-----------|-------:|
| slow_zoom_in | 30 |
| slow_zoom_out | 20 |
| ken_burns | 25 |
| pan_left | 10 |
| pan_right | 10 |
| static | 5 |

### Mood map (from File 07/11)

Examples: `dramatic`→`dramatic_zoom_in`, `mysterious`/`ominous`→`slow_zoom_in`, `solemn`/`calm`→`slow_zoom_out`, `historical`→`pan_left`, `document`→`vertical_scan`, default→`ken_burns`.

---

## 4. FFmpeg filter shape

```text
zoompan=z=<zoom_expr>:x=<x_expr>:y=<y_expr>:d=<total_frames>:s=1920x1080:fps=<fps>
```

- `total_frames = int(duration_seconds * fps)` (default fps 30)
- Zoom/pan expressions use frame number `on` (FFmpeg zoompan)
- Easing: prefer **ease_in_out** via expression math where practical; document if simplified linear for v1

**Config work in B.9:** Replace `PLACEHOLDER_PHASE_B` in `config/animation_presets.json` with structured fields (zoom_start/end, pan modes, easing, weights) — same approach as transitions/SFX.

---

## 5. Files to create (when coding starts)

| File | Role |
|------|------|
| `modules/animation_engine.py` | Main BaseModule |
| `modules/animation_presets_data.py` (optional) | Constants if class would exceed 300 lines |
| `config/animation_presets.json` | Real preset content (merge File 11) |
| `tests/unit/test_animation_engine.py` | Unit tests |
| Update `modules_config.json` | Already has `animation_engine` entry |

**No OpenCV dependency** in first ship unless you explicitly want it after Windows tests.

---

## 6. Planned tests (~14–18)

| # | Test | Intent |
|---|------|--------|
| 1 | All preset ids load | Catalog completeness |
| 2 | `get_zoompan_filter` ken_burns | Contains `zoompan=`, `d=`, `fps=` |
| 3 | slow_zoom_in / out | Distinct zoom direction |
| 4 | pan_left / pan_right | x expression differs |
| 5 | static | zoom start≈end |
| 6 | intensity subtle vs dramatic | Dramatic larger zoom delta |
| 7 | duration → frame count | `d=duration*fps` |
| 8 | unknown type → ken_burns default + warning | Validation |
| 9 | weighted random distribution | Statistical smoke (many draws) |
| 10 | mood map | dramatic → dramatic_zoom_in |
| 11 | batch from timeline | N scenes → N filters |
| 12 | invalid duration | clamp / error |
| 13 | default resolution 1920x1080 | `s=1920x1080` |
| 14 | module disable flag | if applicable |
| 15 | integration smoke | timeline scene → non-empty filter string |

**No real FFmpeg render required for unit tests** (string generation only), same pattern as `transition_engine`.

---

## 7. Effort estimate

| Item | Estimate |
|------|----------|
| `animation_engine.py` | **350–550** LOC (may split presets → keep classes ≤300) |
| Preset data / config merge | **150–250** LOC JSON + small loader |
| Unit tests | **250–350** LOC, **14–18** tests |
| Total new code | **~750–1,150** LOC |
| Calendar time (agent) | **~0.5–1 day** focused work |
| Risk | **High** for *visual* quality later; **Medium** for unit-testable filter builder |

Comparable modules:

| Module | Lines (approx) |
|--------|----------------|
| transition_engine | ~524 |
| timeline_engine | ~741 |
| Expected animation_engine package | ~similar to transition |

---

## 8. Dependencies

| Dependency | B.9 unit tests | Real scene render |
|------------|----------------|-------------------|
| FFmpeg | Not required | Required (export_engine) |
| OpenCV | Not required (v1) | Optional later |
| numpy | Optional | Optional |
| Pillow | Only if image size probe | Useful for real sizes |

---

## 9. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| zoompan expression bugs (black frames, out-of-bounds pan) | Clamp pan to valid range; unit-test expression structure; visual check only when FFmpeg available |
| Class >300 lines | Split `animation_presets_data.py` / helpers |
| Over-scoping OpenCV smart Ken Burns | Defer to DEBT-B9a |
| Config still placeholders | Merge File 11 content in B.9 as first coding step |

---

## 10. Out of scope for B.9

- Actually rendering MP4 (that’s **B.12 export_engine**)
- Combining grade + animation in one FFmpeg graph (export)
- UI animation picker (Phase C)
- OpenCV face/saliency detection (unless approved later)

---

## 11. Ready checklist before coding B.9

- [ ] Windows pytest results reviewed  
- [ ] User explicitly approves B.9 coding  
- [ ] Safety net: `Autopilot_Backup_B8_final.zip` saved  
- [ ] DEBT-B1 done (FileParser)  

**Not coding until you approve after Windows tests.**

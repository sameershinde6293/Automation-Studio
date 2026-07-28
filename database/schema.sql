-- ============================================================
-- AUTOPILOT DATABASE SCHEMA
-- Version: 1.0.0
-- Product tables: 25
-- Infrastructure: schema_migrations
-- ============================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = 10000;
PRAGMA temp_store = MEMORY;

-- ============================================================
-- TABLE 1: projects
-- PURPOSE: Main project record, one row per project
-- ============================================================
CREATE TABLE IF NOT EXISTS projects (
    id                      TEXT PRIMARY KEY,
    title                   TEXT NOT NULL,
    description             TEXT DEFAULT '',
    channel_profile_id      TEXT DEFAULT 'default',
    genre                   TEXT DEFAULT 'dark_history',
    status                  TEXT DEFAULT 'new',
    -- Status values: new, importing, processing, tts_generating,
    --                rendering, completed, failed, cancelled
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    last_opened_at          TEXT DEFAULT NULL,
    project_folder_path     TEXT NOT NULL,
    script_file_path        TEXT DEFAULT NULL,
    script_format           TEXT DEFAULT NULL,
    -- Format values: txt, json, csv, docx, pdf
    total_scenes            INTEGER DEFAULT 0,
    total_duration_seconds  REAL DEFAULT 0.0,
    total_images            INTEGER DEFAULT 0,
    narration_duration      REAL DEFAULT 0.0,
    export_preset           TEXT DEFAULT 'youtube_1080p',
    color_grade_preset      TEXT DEFAULT 'dark_moody',
    default_transition      TEXT DEFAULT 'crossfade',
    default_animation       TEXT DEFAULT 'ken_burns',
    default_subtitle_style  TEXT DEFAULT 'word_by_word',
    music_file_path         TEXT DEFAULT NULL,
    music_volume            REAL DEFAULT 0.40,
    narration_volume        REAL DEFAULT 1.00,
    sfx_volume              REAL DEFAULT 0.60,
    has_intro               INTEGER DEFAULT 1,
    has_outro               INTEGER DEFAULT 1,
    has_watermark           INTEGER DEFAULT 1,
    has_subtitles           INTEGER DEFAULT 1,
    render_count            INTEGER DEFAULT 0,
    last_render_at          TEXT DEFAULT NULL,
    last_render_output_path TEXT DEFAULT NULL,
    notes                   TEXT DEFAULT '',
    tags                    TEXT DEFAULT '',
    version                 INTEGER DEFAULT 1
);

-- ============================================================
-- TABLE 2: scenes
-- PURPOSE: One row per scene in project
-- ============================================================
CREATE TABLE IF NOT EXISTS scenes (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    scene_number            INTEGER NOT NULL,
    scene_title             TEXT DEFAULT '',
    image_filename          TEXT DEFAULT NULL,
    image_file_path         TEXT DEFAULT NULL,
    image_matched           INTEGER DEFAULT 0,
    image_match_confidence  REAL DEFAULT 0.0,
    proxy_image_path        TEXT DEFAULT NULL,
    start_time              REAL DEFAULT 0.0,
    end_time                REAL DEFAULT 0.0,
    duration                REAL DEFAULT 0.0,
    transition_in           TEXT DEFAULT 'crossfade',
    transition_out          TEXT DEFAULT 'crossfade',
    transition_duration     REAL DEFAULT 0.8,
    animation_type          TEXT DEFAULT 'ken_burns',
    animation_intensity     TEXT DEFAULT 'medium',
    color_grade_override    TEXT DEFAULT NULL,
    sfx_trigger             TEXT DEFAULT NULL,
    caption_text            TEXT DEFAULT '',
    scene_notes             TEXT DEFAULT '',
    keyword_mood            TEXT DEFAULT NULL,
    is_chapter_start        INTEGER DEFAULT 0,
    chapter_title           TEXT DEFAULT '',
    is_title_card           INTEGER DEFAULT 0,
    title_card_text         TEXT DEFAULT '',
    status                  TEXT DEFAULT 'pending',
    -- Status: pending, processing, completed, error
    error_message           TEXT DEFAULT NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 3: dialogue_lines
-- PURPOSE: One row per line of dialogue in script
-- ============================================================
CREATE TABLE IF NOT EXISTS dialogue_lines (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    scene_id                TEXT NOT NULL,
    line_number             INTEGER NOT NULL,
    character_name          TEXT NOT NULL,
    emotion                 TEXT DEFAULT 'neutral',
    speed                   REAL DEFAULT 1.0,
    pitch                   REAL DEFAULT 0.0,
    volume                  REAL DEFAULT 1.0,
    pause_before            TEXT DEFAULT 'none',
    pause_after             TEXT DEFAULT 'short',
    -- Pause values: none, micro, short, medium, long, dramatic
    text_content            TEXT NOT NULL,
    audio_file_path         TEXT DEFAULT NULL,
    audio_generated         INTEGER DEFAULT 0,
    audio_duration          REAL DEFAULT 0.0,
    word_timestamps_json    TEXT DEFAULT NULL,
    -- JSON array of {word, start_time, end_time} objects
    generation_engine       TEXT DEFAULT NULL,
    generation_voice        TEXT DEFAULT NULL,
    generation_attempts     INTEGER DEFAULT 0,
    status                  TEXT DEFAULT 'pending',
    -- Status: pending, generating, completed, error
    error_message           TEXT DEFAULT NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 4: voice_profiles
-- PURPOSE: Auto-created character voice profiles per project
-- ============================================================
CREATE TABLE IF NOT EXISTS voice_profiles (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    character_name          TEXT NOT NULL,
    character_aliases       TEXT DEFAULT '',
    -- Comma separated list of aliases eg "NARR,N,NARRATOR"
    voice_model             TEXT DEFAULT NULL,
    engine                  TEXT DEFAULT 'kokoro',
    -- Engine values: piper, kokoro, xtts
    default_emotion         TEXT DEFAULT 'neutral',
    speed                   REAL DEFAULT 1.0,
    pitch                   REAL DEFAULT 0.0,
    volume                  REAL DEFAULT 1.0,
    reverb_preset           TEXT DEFAULT 'none',
    echo_preset             TEXT DEFAULT 'none',
    breathing_enabled       INTEGER DEFAULT 0,
    breathing_volume        REAL DEFAULT 0.15,
    pause_sentence          REAL DEFAULT 0.6,
    pause_paragraph         REAL DEFAULT 1.8,
    pause_comma             REAL DEFAULT 0.2,
    eq_preset               TEXT DEFAULT 'documentary_male',
    compression_enabled     INTEGER DEFAULT 1,
    noise_gate_enabled      INTEGER DEFAULT 1,
    de_esser_enabled        INTEGER DEFAULT 1,
    special_effect          TEXT DEFAULT 'none',
    -- Effect values: none, old_radio, telephone, megaphone,
    --                underground, ghost, god, demon, distant
    is_auto_created         INTEGER DEFAULT 1,
    color_label             TEXT DEFAULT '#4A90D9',
    avatar_path             TEXT DEFAULT NULL,
    role_description        TEXT DEFAULT '',
    total_lines_generated   INTEGER DEFAULT 0,
    total_audio_duration    REAL DEFAULT 0.0,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    UNIQUE(project_id, character_name)
);

-- ============================================================
-- TABLE 5: channel_profiles
-- PURPOSE: Saved channel profiles with all default settings
-- ============================================================
CREATE TABLE IF NOT EXISTS channel_profiles (
    id                      TEXT PRIMARY KEY,
    profile_name            TEXT NOT NULL UNIQUE,
    channel_name            TEXT DEFAULT '',
    channel_logo_path       TEXT DEFAULT NULL,
    genre                   TEXT DEFAULT 'dark_history',
    color_primary           TEXT DEFAULT '#FFFFFF',
    color_secondary         TEXT DEFAULT '#FF0000',
    color_accent            TEXT DEFAULT '#FFD700',
    default_font            TEXT DEFAULT 'Montserrat-Bold',
    default_color_grade     TEXT DEFAULT 'dark_moody',
    default_animation       TEXT DEFAULT 'ken_burns',
    default_transition      TEXT DEFAULT 'crossfade',
    default_export_preset   TEXT DEFAULT 'youtube_1080p',
    default_subtitle_style  TEXT DEFAULT 'word_by_word',
    subtitle_font           TEXT DEFAULT 'Montserrat-Bold',
    subtitle_font_size      INTEGER DEFAULT 52,
    subtitle_color          TEXT DEFAULT '#FFFFFF',
    subtitle_outline_color  TEXT DEFAULT '#000000',
    subtitle_outline_size   INTEGER DEFAULT 3,
    subtitle_bg_enabled     INTEGER DEFAULT 1,
    subtitle_bg_color       TEXT DEFAULT '#000000',
    subtitle_bg_opacity     REAL DEFAULT 0.5,
    subtitle_position       TEXT DEFAULT 'bottom',
    watermark_enabled       INTEGER DEFAULT 1,
    watermark_path          TEXT DEFAULT NULL,
    watermark_position      TEXT DEFAULT 'bottom_right',
    watermark_opacity       REAL DEFAULT 0.20,
    watermark_size          REAL DEFAULT 0.08,
    music_folder_path       TEXT DEFAULT NULL,
    music_volume            REAL DEFAULT 0.40,
    narration_volume        REAL DEFAULT 1.00,
    sfx_volume              REAL DEFAULT 0.60,
    ducking_depth           REAL DEFAULT 0.15,
    ducking_ceiling         REAL DEFAULT 0.50,
    ducking_attack          REAL DEFAULT 0.30,
    ducking_release         REAL DEFAULT 0.80,
    intro_enabled           INTEGER DEFAULT 1,
    intro_template          TEXT DEFAULT 'dark_history',
    intro_duration          REAL DEFAULT 5.0,
    intro_custom_path       TEXT DEFAULT NULL,
    outro_enabled           INTEGER DEFAULT 1,
    outro_template          TEXT DEFAULT 'dark_history',
    outro_duration          REAL DEFAULT 20.0,
    outro_custom_path       TEXT DEFAULT NULL,
    thumbnail_style         TEXT DEFAULT 'dark_history',
    social_youtube          TEXT DEFAULT '',
    social_instagram        TEXT DEFAULT '',
    social_twitter          TEXT DEFAULT '',
    patreon_link            TEXT DEFAULT '',
    copyright_text          TEXT DEFAULT '',
    is_default              INTEGER DEFAULT 0,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

-- ============================================================
-- TABLE 6: render_progress
-- PURPOSE: Track render progress for crash recovery
-- ============================================================
CREATE TABLE IF NOT EXISTS render_progress (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL UNIQUE,
    render_session_id       TEXT NOT NULL,
    current_stage           TEXT NOT NULL,
    -- Stage values: started, parsing, image_processing,
    --               tts_generating, audio_mixing, rendering,
    --               subtitle_burning, joining, exporting, completed
    stage_percent           REAL DEFAULT 0.0,
    current_scene_id        TEXT DEFAULT NULL,
    current_scene_number    INTEGER DEFAULT 0,
    total_scenes            INTEGER DEFAULT 0,
    completed_scenes_json   TEXT DEFAULT '[]',
    -- JSON array of completed scene IDs
    failed_scenes_json      TEXT DEFAULT '[]',
    tts_completed_lines     TEXT DEFAULT '[]',
    -- JSON array of completed dialogue line IDs
    segment_files_json      TEXT DEFAULT '[]',
    -- JSON array of completed segment file paths
    started_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    estimated_completion    TEXT DEFAULT NULL,
    render_settings_json    TEXT DEFAULT '{}',
    -- Complete render settings snapshot for resume
    error_count             INTEGER DEFAULT 0,
    last_error              TEXT DEFAULT NULL,
    is_resumable            INTEGER DEFAULT 1,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 7: render_history
-- PURPOSE: Record of all completed renders per project
-- ============================================================
CREATE TABLE IF NOT EXISTS render_history (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    render_session_id       TEXT NOT NULL,
    started_at              TEXT NOT NULL,
    completed_at            TEXT DEFAULT NULL,
    duration_seconds        REAL DEFAULT 0.0,
    -- How long the render took
    video_duration_seconds  REAL DEFAULT 0.0,
    -- Duration of the output video
    output_file_path        TEXT DEFAULT NULL,
    output_file_size_bytes  INTEGER DEFAULT 0,
    output_resolution       TEXT DEFAULT NULL,
    output_fps              INTEGER DEFAULT 30,
    output_codec            TEXT DEFAULT 'h264',
    export_preset_used      TEXT DEFAULT NULL,
    color_grade_used        TEXT DEFAULT NULL,
    scenes_rendered         INTEGER DEFAULT 0,
    tts_lines_generated     INTEGER DEFAULT 0,
    status                  TEXT DEFAULT 'completed',
    -- Status: completed, failed, cancelled
    error_message           TEXT DEFAULT NULL,
    render_log_path         TEXT DEFAULT NULL,
    settings_snapshot_json  TEXT DEFAULT '{}',
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 8: timeline_data
-- PURPOSE: Complete timeline structure for project
-- ============================================================
CREATE TABLE IF NOT EXISTS timeline_data (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL UNIQUE,
    total_duration          REAL DEFAULT 0.0,
    intro_duration          REAL DEFAULT 0.0,
    outro_duration          REAL DEFAULT 0.0,
    content_duration        REAL DEFAULT 0.0,
    narration_duration      REAL DEFAULT 0.0,
    music_start_time        REAL DEFAULT 0.0,
    music_end_time          REAL DEFAULT 0.0,
    chapter_markers_json    TEXT DEFAULT '[]',
    -- JSON array of {timestamp, label, scene_id} objects
    youtube_chapters_text   TEXT DEFAULT '',
    -- Ready to paste YouTube chapter text
    timeline_json           TEXT DEFAULT '{}',
    -- Complete timeline structure as JSON
    is_valid                INTEGER DEFAULT 0,
    validation_errors_json  TEXT DEFAULT '[]',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 9: audio_tracks
-- PURPOSE: All audio files associated with project
-- ============================================================
CREATE TABLE IF NOT EXISTS audio_tracks (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    track_type              TEXT NOT NULL,
    -- Type values: narration, music, sfx, ambient, clone_sample
    file_path               TEXT NOT NULL,
    file_name               TEXT NOT NULL,
    duration_seconds        REAL DEFAULT 0.0,
    sample_rate             INTEGER DEFAULT 48000,
    channels                INTEGER DEFAULT 2,
    bitrate                 INTEGER DEFAULT 192,
    format                  TEXT DEFAULT 'mp3',
    volume                  REAL DEFAULT 1.0,
    fade_in_duration        REAL DEFAULT 0.0,
    fade_out_duration       REAL DEFAULT 0.0,
    start_offset            REAL DEFAULT 0.0,
    is_loop                 INTEGER DEFAULT 0,
    is_normalized           INTEGER DEFAULT 0,
    peak_db                 REAL DEFAULT NULL,
    lufs                    REAL DEFAULT NULL,
    status                  TEXT DEFAULT 'imported',
    created_at              TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 10: subtitle_data
-- PURPOSE: Subtitle information per project
-- ============================================================
CREATE TABLE IF NOT EXISTS subtitle_data (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL UNIQUE,
    source_type             TEXT DEFAULT 'generated',
    -- Source: generated, imported, both
    imported_file_path      TEXT DEFAULT NULL,
    generated_file_path     TEXT DEFAULT NULL,
    final_file_path         TEXT DEFAULT NULL,
    style_preset            TEXT DEFAULT 'word_by_word',
    font_family             TEXT DEFAULT 'Montserrat-Bold',
    font_size               INTEGER DEFAULT 52,
    font_color              TEXT DEFAULT '#FFFFFF',
    outline_color           TEXT DEFAULT '#000000',
    outline_size            INTEGER DEFAULT 3,
    shadow_enabled          INTEGER DEFAULT 1,
    shadow_color            TEXT DEFAULT '#000000',
    shadow_offset_x         INTEGER DEFAULT 2,
    shadow_offset_y         INTEGER DEFAULT 2,
    background_enabled      INTEGER DEFAULT 1,
    background_color        TEXT DEFAULT '#000000',
    background_opacity      REAL DEFAULT 0.5,
    background_radius       INTEGER DEFAULT 8,
    position                TEXT DEFAULT 'bottom',
    vertical_margin         INTEGER DEFAULT 60,
    horizontal_margin       INTEGER DEFAULT 80,
    max_chars_per_line      INTEGER DEFAULT 42,
    max_lines               INTEGER DEFAULT 2,
    highlight_color         TEXT DEFAULT '#FFD700',
    -- Used for word by word highlight style
    fade_duration           REAL DEFAULT 0.1,
    animation_style         TEXT DEFAULT 'pop',
    -- Animation for word appearance: pop, fade, slide_up
    total_subtitles         INTEGER DEFAULT 0,
    total_words             INTEGER DEFAULT 0,
    sync_offset_ms          INTEGER DEFAULT 0,
    is_burned               INTEGER DEFAULT 0,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 11: word_timestamps
-- PURPOSE: Word level timing from TTS for animated subtitles
-- ============================================================
CREATE TABLE IF NOT EXISTS word_timestamps (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    dialogue_line_id        TEXT NOT NULL,
    word_index              INTEGER NOT NULL,
    word_text               TEXT NOT NULL,
    start_time_ms           INTEGER NOT NULL,
    end_time_ms             INTEGER NOT NULL,
    confidence              REAL DEFAULT 1.0,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (dialogue_line_id) REFERENCES dialogue_lines(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 12: sfx_placements
-- PURPOSE: Sound effect placement on timeline
-- ============================================================
CREATE TABLE IF NOT EXISTS sfx_placements (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    scene_id                TEXT DEFAULT NULL,
    sfx_name                TEXT NOT NULL,
    sfx_file_path           TEXT NOT NULL,
    placement_type          TEXT DEFAULT 'manual',
    -- Type: manual, auto_transition, auto_keyword, auto_chapter
    timestamp_seconds       REAL NOT NULL,
    volume                  REAL DEFAULT 0.7,
    fade_in                 REAL DEFAULT 0.1,
    fade_out                REAL DEFAULT 0.3,
    trigger_keyword         TEXT DEFAULT NULL,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 13: image_assets
-- PURPOSE: All images associated with project
-- ============================================================
CREATE TABLE IF NOT EXISTS image_assets (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    original_file_path      TEXT NOT NULL,
    original_filename       TEXT NOT NULL,
    processed_file_path     TEXT DEFAULT NULL,
    proxy_file_path         TEXT DEFAULT NULL,
    width                   INTEGER DEFAULT 0,
    height                  INTEGER DEFAULT 0,
    aspect_ratio            TEXT DEFAULT NULL,
    orientation             TEXT DEFAULT 'landscape',
    -- Orientation: landscape, portrait, square
    file_size_bytes         INTEGER DEFAULT 0,
    format                  TEXT DEFAULT NULL,
    is_processed            INTEGER DEFAULT 0,
    is_proxy_generated      INTEGER DEFAULT 0,
    processing_applied      TEXT DEFAULT '',
    -- Comma separated list of processing applied
    -- eg: resize,crop,blur_background,rotate
    exif_date               TEXT DEFAULT NULL,
    exif_gps                TEXT DEFAULT NULL,
    brightness_score        REAL DEFAULT NULL,
    contrast_score          REAL DEFAULT NULL,
    is_low_resolution       INTEGER DEFAULT 0,
    warning_message         TEXT DEFAULT NULL,
    used_in_scenes          TEXT DEFAULT '',
    -- Comma separated scene IDs where this image is used
    created_at              TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 14: installed_voices
-- PURPOSE: Track all installed TTS voice models
-- ============================================================
CREATE TABLE IF NOT EXISTS installed_voices (
    id                      TEXT PRIMARY KEY,
    voice_name              TEXT NOT NULL,
    voice_display_name      TEXT NOT NULL,
    engine                  TEXT NOT NULL,
    -- Engine: piper, kokoro, xtts
    language                TEXT DEFAULT 'en',
    accent                  TEXT DEFAULT 'us',
    gender                  TEXT DEFAULT 'male',
    style                   TEXT DEFAULT 'documentary',
    quality_rating          INTEGER DEFAULT 4,
    -- Rating 1 to 5
    model_file_path         TEXT NOT NULL,
    config_file_path        TEXT DEFAULT NULL,
    model_size_mb           REAL DEFAULT 0.0,
    ram_required_mb         INTEGER DEFAULT 512,
    supported_emotions      TEXT DEFAULT '',
    -- Comma separated list of supported emotions
    is_cloned               INTEGER DEFAULT 0,
    clone_sample_path       TEXT DEFAULT NULL,
    installed_at            TEXT NOT NULL,
    last_used_at            TEXT DEFAULT NULL,
    total_uses              INTEGER DEFAULT 0,
    is_enabled              INTEGER DEFAULT 1,
    store_voice_id          TEXT DEFAULT NULL,
    -- Reference to voice store catalog entry
    UNIQUE(engine, voice_name)
);

-- ============================================================
-- TABLE 15: cloned_voices
-- PURPOSE: Track user created cloned voices
-- ============================================================
CREATE TABLE IF NOT EXISTS cloned_voices (
    id                      TEXT PRIMARY KEY,
    voice_name              TEXT NOT NULL UNIQUE,
    display_name            TEXT NOT NULL,
    sample_file_path        TEXT NOT NULL,
    sample_duration_seconds REAL DEFAULT 0.0,
    sample_quality_score    REAL DEFAULT 0.0,
    -- Quality score 0.0 to 1.0
    model_file_path         TEXT DEFAULT NULL,
    engine                  TEXT DEFAULT 'xtts',
    similarity_score        REAL DEFAULT 0.0,
    -- How similar clone is to original 0.0 to 1.0
    is_ready                INTEGER DEFAULT 0,
    creation_attempts       INTEGER DEFAULT 0,
    notes                   TEXT DEFAULT '',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

-- ============================================================
-- TABLE 16: batch_queue
-- PURPOSE: Batch rendering queue
-- ============================================================
CREATE TABLE IF NOT EXISTS batch_queue (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT DEFAULT NULL,
    project_folder_path     TEXT NOT NULL,
    project_title           TEXT DEFAULT 'Unknown',
    channel_profile_id      TEXT DEFAULT 'default',
    priority                INTEGER DEFAULT 5,
    -- Priority 1 (highest) to 10 (lowest)
    status                  TEXT DEFAULT 'queued',
    -- Status: queued, processing, completed, failed, cancelled, paused
    added_at                TEXT NOT NULL,
    started_at              TEXT DEFAULT NULL,
    completed_at            TEXT DEFAULT NULL,
    output_file_path        TEXT DEFAULT NULL,
    error_message           TEXT DEFAULT NULL,
    retry_count             INTEGER DEFAULT 0,
    max_retries             INTEGER DEFAULT 3,
    estimated_duration_min  REAL DEFAULT NULL,
    notes                   TEXT DEFAULT ''
);

-- ============================================================
-- TABLE 17: quality_check_results
-- PURPOSE: Store pre-render quality check results
-- ============================================================
CREATE TABLE IF NOT EXISTS quality_check_results (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    check_timestamp         TEXT NOT NULL,
    total_checks            INTEGER DEFAULT 0,
    passed_checks           INTEGER DEFAULT 0,
    failed_checks           INTEGER DEFAULT 0,
    warning_count           INTEGER DEFAULT 0,
    auto_fixed_count        INTEGER DEFAULT 0,
    issues_json             TEXT DEFAULT '[]',
    -- JSON array of issue objects with type, severity, description, fix
    is_render_ready         INTEGER DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 18: license_data
-- PURPOSE: License and activation data
-- ============================================================
CREATE TABLE IF NOT EXISTS license_data (
    id                      INTEGER PRIMARY KEY DEFAULT 1,
    -- Only one row ever in this table
    hwid                    TEXT NOT NULL,
    license_key             TEXT DEFAULT NULL,
    activation_date         TEXT DEFAULT NULL,
    expiry_date             TEXT DEFAULT NULL,
    days_granted            INTEGER DEFAULT 0,
    days_remaining          INTEGER DEFAULT 0,
    is_activated            INTEGER DEFAULT 0,
    is_trial                INTEGER DEFAULT 1,
    trial_start_date        TEXT DEFAULT NULL,
    trial_days              INTEGER DEFAULT 30,
    activation_count        INTEGER DEFAULT 0,
    last_check_date         TEXT DEFAULT NULL,
    tamper_hash             TEXT DEFAULT NULL,
    -- Hash of all fields to detect tampering
    user_note               TEXT DEFAULT NULL
    -- Optional user name or note from key generator
);

-- ============================================================
-- TABLE 19: app_settings
-- PURPOSE: All application settings stored in database
-- ============================================================
CREATE TABLE IF NOT EXISTS app_settings (
    key                     TEXT PRIMARY KEY,
    value                   TEXT NOT NULL,
    value_type              TEXT DEFAULT 'string',
    -- Type: string, integer, real, boolean, json
    category                TEXT DEFAULT 'general',
    description             TEXT DEFAULT '',
    updated_at              TEXT NOT NULL
);

-- ============================================================
-- TABLE 20: app_logs
-- PURPOSE: Application event log stored in database
-- ============================================================
CREATE TABLE IF NOT EXISTS app_logs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp               TEXT NOT NULL,
    level                   TEXT NOT NULL,
    -- Level: debug, info, warning, error, critical
    module                  TEXT NOT NULL,
    message                 TEXT NOT NULL,
    project_id              TEXT DEFAULT NULL,
    scene_id                TEXT DEFAULT NULL,
    extra_data_json         TEXT DEFAULT NULL
);

-- ============================================================
-- TABLE 21: render_log_entries
-- PURPOSE: Detailed render log per render session
-- ============================================================
CREATE TABLE IF NOT EXISTS render_log_entries (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    render_session_id       TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    timestamp               TEXT NOT NULL,
    stage                   TEXT NOT NULL,
    message                 TEXT NOT NULL,
    scene_id                TEXT DEFAULT NULL,
    duration_ms             INTEGER DEFAULT 0,
    is_error                INTEGER DEFAULT 0,
    ffmpeg_command          TEXT DEFAULT NULL
);

-- ============================================================
-- TABLE 22: thumbnails
-- PURPOSE: Generated thumbnail information
-- ============================================================
CREATE TABLE IF NOT EXISTS thumbnails (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    render_session_id       TEXT DEFAULT NULL,
    variation_number        INTEGER NOT NULL,
    -- 1 through 5
    file_path               TEXT NOT NULL,
    source_timestamp        REAL DEFAULT 0.0,
    style_applied           TEXT DEFAULT NULL,
    title_text              TEXT DEFAULT NULL,
    channel_text            TEXT DEFAULT NULL,
    file_size_bytes         INTEGER DEFAULT 0,
    width                   INTEGER DEFAULT 1280,
    height                  INTEGER DEFAULT 720,
    is_selected             INTEGER DEFAULT 0,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- ============================================================
-- TABLE 23: recent_projects
-- PURPOSE: Track recently opened projects for quick access
-- ============================================================
CREATE TABLE IF NOT EXISTS recent_projects (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id              TEXT NOT NULL,
    project_title           TEXT NOT NULL,
    project_folder_path     TEXT NOT NULL,
    thumbnail_path          TEXT DEFAULT NULL,
    last_opened_at          TEXT NOT NULL,
    open_count              INTEGER DEFAULT 1
);

-- ============================================================
-- TABLE 24: voice_store_cache
-- PURPOSE: Cache voice store catalog locally
-- ============================================================
CREATE TABLE IF NOT EXISTS voice_store_cache (
    id                      TEXT PRIMARY KEY,
    voice_name              TEXT NOT NULL,
    display_name            TEXT NOT NULL,
    engine                  TEXT NOT NULL,
    language                TEXT DEFAULT 'en',
    accent                  TEXT DEFAULT 'us',
    gender                  TEXT DEFAULT 'male',
    style                   TEXT DEFAULT 'documentary',
    quality_rating          INTEGER DEFAULT 4,
    download_url            TEXT NOT NULL,
    preview_url             TEXT DEFAULT NULL,
    file_size_mb            REAL DEFAULT 0.0,
    ram_required_mb         INTEGER DEFAULT 512,
    supported_emotions      TEXT DEFAULT '',
    description             TEXT DEFAULT '',
    tags                    TEXT DEFAULT '',
    is_installed            INTEGER DEFAULT 0,
    is_featured             INTEGER DEFAULT 0,
    catalog_updated_at      TEXT NOT NULL
);

-- ============================================================
-- TABLE 25: engine_installations
-- PURPOSE: Track installed TTS engines
-- ============================================================
CREATE TABLE IF NOT EXISTS engine_installations (
    id                      TEXT PRIMARY KEY,
    engine_name             TEXT NOT NULL UNIQUE,
    -- Name: piper, kokoro, xtts, styletts2, openvoice
    display_name            TEXT NOT NULL,
    version                 TEXT DEFAULT NULL,
    install_path            TEXT NOT NULL,
    executable_path         TEXT DEFAULT NULL,
    is_installed            INTEGER DEFAULT 0,
    is_enabled              INTEGER DEFAULT 1,
    ram_required_mb         INTEGER DEFAULT 512,
    supports_emotions       INTEGER DEFAULT 0,
    supports_cloning        INTEGER DEFAULT 0,
    installed_at            TEXT DEFAULT NULL,
    last_used_at            TEXT DEFAULT NULL,
    total_generations       INTEGER DEFAULT 0,
    total_audio_seconds     REAL DEFAULT 0.0,
    status                  TEXT DEFAULT 'not_installed',
    -- Status: not_installed, installing, installed, error, disabled
    error_message           TEXT DEFAULT NULL
);


-- Projects indexes
CREATE INDEX IF NOT EXISTS idx_projects_status
    ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_created
    ON projects(created_at);

-- Scenes indexes
CREATE INDEX IF NOT EXISTS idx_scenes_project
    ON scenes(project_id);
CREATE INDEX IF NOT EXISTS idx_scenes_number
    ON scenes(project_id, scene_number);
CREATE INDEX IF NOT EXISTS idx_scenes_status
    ON scenes(status);

-- Dialogue lines indexes
CREATE INDEX IF NOT EXISTS idx_dialogue_project
    ON dialogue_lines(project_id);
CREATE INDEX IF NOT EXISTS idx_dialogue_scene
    ON dialogue_lines(scene_id);
CREATE INDEX IF NOT EXISTS idx_dialogue_status
    ON dialogue_lines(status);
CREATE INDEX IF NOT EXISTS idx_dialogue_character
    ON dialogue_lines(character_name);

-- Word timestamps indexes
CREATE INDEX IF NOT EXISTS idx_words_project
    ON word_timestamps(project_id);
CREATE INDEX IF NOT EXISTS idx_words_line
    ON word_timestamps(dialogue_line_id);
CREATE INDEX IF NOT EXISTS idx_words_time
    ON word_timestamps(start_time_ms);

-- Audio tracks indexes
CREATE INDEX IF NOT EXISTS idx_audio_project
    ON audio_tracks(project_id);
CREATE INDEX IF NOT EXISTS idx_audio_type
    ON audio_tracks(track_type);

-- Image assets indexes
CREATE INDEX IF NOT EXISTS idx_images_project
    ON image_assets(project_id);
CREATE INDEX IF NOT EXISTS idx_images_filename
    ON image_assets(original_filename);

-- Render progress indexes
CREATE INDEX IF NOT EXISTS idx_render_project
    ON render_progress(project_id);

-- Render history indexes
CREATE INDEX IF NOT EXISTS idx_history_project
    ON render_history(project_id);

-- SFX placements indexes
CREATE INDEX IF NOT EXISTS idx_sfx_project
    ON sfx_placements(project_id);
CREATE INDEX IF NOT EXISTS idx_sfx_timestamp
    ON sfx_placements(timestamp_seconds);

-- App logs indexes
CREATE INDEX IF NOT EXISTS idx_logs_timestamp
    ON app_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_logs_level
    ON app_logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_module
    ON app_logs(module);

-- Batch queue indexes
CREATE INDEX IF NOT EXISTS idx_batch_status
    ON batch_queue(status);
CREATE INDEX IF NOT EXISTS idx_batch_priority
    ON batch_queue(priority);

-- Voice profiles indexes
CREATE INDEX IF NOT EXISTS idx_voice_project
    ON voice_profiles(project_id);
CREATE INDEX IF NOT EXISTS idx_voice_character
    ON voice_profiles(character_name);

-- Recent projects indexes
CREATE INDEX IF NOT EXISTS idx_recent_opened
    ON recent_projects(last_opened_at DESC);


-- Insert default app settings on first run
INSERT OR IGNORE INTO app_settings (key, value, value_type, category, description, updated_at) VALUES
('theme', 'dark', 'string', 'ui', 'Application theme', datetime('now')),
('language', 'en', 'string', 'general', 'Application language', datetime('now')),
('auto_save_interval', '300', 'integer', 'general', 'Auto save interval in seconds', datetime('now')),
('max_ram_mb', '6144', 'integer', 'performance', 'Maximum RAM usage in MB', datetime('now')),
('auto_render_mode', '1', 'boolean', 'workflow', 'Skip review and render automatically', datetime('now')),
('default_channel_profile', 'default', 'string', 'workflow', 'Default channel profile name', datetime('now')),
('hardware_acceleration', 'auto', 'string', 'performance', 'Hardware encoding mode', datetime('now')),
('proxy_resolution', '480', 'integer', 'performance', 'Proxy image height in pixels', datetime('now')),
('cache_size_mb', '2048', 'integer', 'performance', 'Maximum cache size in MB', datetime('now')),
('segment_duration', '30', 'integer', 'render', 'Render segment duration in seconds', datetime('now')),
('checkpoint_interval', '30', 'integer', 'render', 'Checkpoint save interval in seconds', datetime('now')),
('log_level', 'info', 'string', 'general', 'Logging level', datetime('now')),
('show_splash', '1', 'boolean', 'ui', 'Show splash screen on startup', datetime('now')),
('auto_open_output', '1', 'boolean', 'workflow', 'Open output folder after render', datetime('now')),
('auto_play_output', '0', 'boolean', 'workflow', 'Play video after render completes', datetime('now')),
('notify_on_complete', '1', 'boolean', 'workflow', 'Show system notification on render complete', datetime('now')),
('sound_on_complete', '1', 'boolean', 'workflow', 'Play sound when render completes', datetime('now')),
('ffmpeg_path', 'engines/ffmpeg/ffmpeg.exe', 'string', 'paths', 'FFmpeg executable path', datetime('now')),
('ffprobe_path', 'engines/ffmpeg/ffprobe.exe', 'string', 'paths', 'FFprobe executable path', datetime('now')),
('projects_folder', 'projects', 'string', 'paths', 'Projects root folder', datetime('now')),
('output_naming', '{title}_{resolution}_{date}', 'string', 'export', 'Output file naming pattern', datetime('now')),
('subtitle_burn_default', '1', 'boolean', 'subtitle', 'Burn subtitles by default', datetime('now')),
('auto_generate_subtitles', '1', 'boolean', 'subtitle', 'Auto generate subtitles from TTS', datetime('now')),
('auto_generate_thumbnails', '1', 'boolean', 'thumbnail', 'Auto generate thumbnails after render', datetime('now')),
('thumbnail_count', '5', 'integer', 'thumbnail', 'Number of thumbnail variations to generate', datetime('now')),
('ducking_enabled', '1', 'boolean', 'audio', 'Enable automatic music ducking', datetime('now')),
('batch_retry_count', '3', 'integer', 'batch', 'Number of retries for failed batch items', datetime('now')),
('batch_stop_on_error', '0', 'boolean', 'batch', 'Stop batch queue on first error', datetime('now')),
('trial_days', '30', 'integer', 'license', 'Number of trial days', datetime('now'));

-- Insert default license record
INSERT OR IGNORE INTO license_data
(id, hwid, is_trial, trial_days, trial_start_date)
VALUES (1, 'UNINITIALIZED', 1, 30, NULL);

-- Insert default TTS engine records
INSERT OR IGNORE INTO engine_installations
(id, engine_name, display_name, install_path, ram_required_mb, supports_emotions, supports_cloning, status)
VALUES
('eng_piper', 'piper', 'Piper TTS', 'engines/piper', 512, 0, 0, 'not_installed'),
('eng_kokoro', 'kokoro', 'Kokoro TTS', 'engines/kokoro', 1024, 1, 0, 'not_installed'),
('eng_xtts', 'xtts', 'XTTS v2', 'engines/xtts', 4096, 1, 1, 'not_installed');

-- Insert default channel profile
INSERT OR IGNORE INTO channel_profiles
(id, profile_name, channel_name, genre, default_color_grade,
 default_animation, default_transition, default_export_preset,
 default_subtitle_style, is_default, created_at, updated_at)
VALUES
('profile_default', 'default', 'My Channel', 'dark_history',
 'dark_moody', 'ken_burns', 'crossfade', 'youtube_1080p',
 'word_by_word', 1, datetime('now'), datetime('now'));


-- Migration tracking table
-- Used to track which migrations have been applied
-- Allows safe database upgrades in future versions
CREATE TABLE IF NOT EXISTS schema_migrations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_version       TEXT NOT NULL UNIQUE,
    migration_name          TEXT NOT NULL,
    applied_at              TEXT NOT NULL,
    checksum                TEXT DEFAULT NULL
);

-- Record initial schema as migration version 1.0.0
INSERT OR IGNORE INTO schema_migrations
(migration_version, migration_name, applied_at)
VALUES ('1.0.0', 'initial_schema', datetime('now'));

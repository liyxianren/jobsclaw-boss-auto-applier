CREATE TABLE IF NOT EXISTS greetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_url TEXT NOT NULL,
    job_title TEXT,
    company TEXT,
    salary TEXT,
    location TEXT,
    recruiter TEXT,
    message TEXT NOT NULL,
    status TEXT DEFAULT 'pending',  -- pending/sent/failed/skipped/already_contacted
    sent_at TEXT,
    screenshot_path TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(job_url)
);

CREATE TABLE IF NOT EXISTS jd_cache (
    job_url TEXT PRIMARY KEY,
    job_title TEXT,
    company TEXT,
    salary TEXT,
    location TEXT,
    experience TEXT,
    education TEXT,
    description TEXT,
    tags TEXT,
    benefits TEXT,
    recruiter TEXT,
    recruiter_title TEXT,
    match_score TEXT,
    fit INTEGER,
    reasoning TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

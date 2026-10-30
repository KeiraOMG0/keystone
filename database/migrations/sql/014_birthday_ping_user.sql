-- Add ping_user toggle to birthday_settings
ALTER TABLE birthday_settings ADD COLUMN ping_user INTEGER NOT NULL DEFAULT 0;

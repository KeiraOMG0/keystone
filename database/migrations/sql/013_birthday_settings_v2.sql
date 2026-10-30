-- Add add_only and private_confirms to birthday_settings
ALTER TABLE birthday_settings ADD COLUMN add_only INTEGER NOT NULL DEFAULT 0;
ALTER TABLE birthday_settings ADD COLUMN private_confirms INTEGER NOT NULL DEFAULT 0;

ALTER TABLE user_navigation_state
ADD COLUMN last_screen_message_id INTEGER
CHECK (last_screen_message_id IS NULL OR last_screen_message_id > 0);

ALTER TABLE user_navigation_state
ADD COLUMN last_keyboard_message_id INTEGER
CHECK (last_keyboard_message_id IS NULL OR last_keyboard_message_id > 0);

-- A request for information the customer did not answer in time closes the case for lack of
-- information and tells the customer (TRZ-28 CA3): one more kind of in-app notification.
ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('approved', 'rejected', 'info_requested', 'audit_reversed', 'info_expired'));

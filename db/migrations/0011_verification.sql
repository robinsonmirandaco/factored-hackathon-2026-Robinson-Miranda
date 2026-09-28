-- Read-back after acting (TRZ-19). A dispute whose read-back does not match what was asked for
-- is kept as evidence for the analyst, with status verification_failed, so it is not a valid
-- dispute and the open-dispute rule (which counts only opened ones) leaves it out.
GRANT UPDATE (status) ON disputes TO trazo_app;

-- Demo state (TRZ-38): a case the demo seed created through the agent is marked simulated, so
-- the database and the screens can tell it from a case a person opened. Every other case is
-- false. trazo_app already updates cases, which is how the seed marks them.
ALTER TABLE cases ADD COLUMN simulated boolean NOT NULL DEFAULT false;

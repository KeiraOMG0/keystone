-- Migration 031: honeypot appeal-flow test cases
--
-- The owner-only Debug page can create a TEST honeypot catch (+ optionally an appeal) for
-- an account (e.g. an alt) without banning anyone, to try the real appeal sign-in,
-- review and one-click rejoin end to end. Those rows carry is_test = 1: they are left out
-- of the catches feed, the stats and the "Caught so far" count, are labelled "Test" in
-- the Appeals queue, and "Delete test data" removes them. Applied once by the runner.
ALTER TABLE honeypot_events ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0;
ALTER TABLE honeypot_appeals ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0;

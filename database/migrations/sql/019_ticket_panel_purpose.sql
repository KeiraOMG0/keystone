-- Migration 019: Ticket panel purpose (support vs. verify)

ALTER TABLE ticket_panels ADD COLUMN purpose TEXT NOT NULL DEFAULT 'support';

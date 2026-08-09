-- Who has already left an App Store review.
--
-- Nothing in the Partner API says. Shopify exposes reviews on the public
-- listing and nowhere else, so this column is hand-maintained: read the dates
-- off your public App Store listing and write them here.
-- Without it the "Ask for a review" call sheet keeps naming merchants who
-- already did, which is the fastest way to make a merchant stop reading.
alter table shops add column if not exists reviewed_at date;

-- Seed your own listing's reviewers by hand once, matched on myshopify domain:
--
--   update shops set reviewed_at = date '2026-04-13'
--   where shop_domain = 'example.myshopify.com';
--
-- Deliberately not seeded by this migration. Review dates are specific to one
-- app's listing, and a migration that writes another deployment's merchants
-- into your database is a bug, not a convenience.

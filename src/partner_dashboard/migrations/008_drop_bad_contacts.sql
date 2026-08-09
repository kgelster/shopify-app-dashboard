-- Clear the merchant contact fields. They were never the merchant.
--
-- Both columns were filled from a third-party CSV export: owner_name from a
-- "Contact 1 Name" column and email from an "Email" column. Contact columns in
-- exports like that list every staff account on the shop, in no meaningful
-- order, which on an app installed by agencies means mostly agencies. One
-- observed shop listed four people, three of them from two different agencies,
-- and its top-level "Email" belonged to an agency rather than the merchant. So the /actions review sheet, headed "who to write to", was naming
-- agency staff as the merchant to email.
--
-- Contact 1 is not the merchant and no other index is either, so there is no
-- mapping that fixes this. The columns are emptied rather than left in place:
-- they are wrong, they are personal data about people who never dealt with
-- this app, and leaving them stored is an invitation to render them again.
-- The columns themselves stay, so a trustworthy source can fill them later.
--
-- Re-importing will NOT refill these, because the CSV importer no longer maps
-- them. If you have a source you trust, add the mapping back deliberately.
update shops set owner_name = null, email = null
where owner_name is not null or email is not null;
